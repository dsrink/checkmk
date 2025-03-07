#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# Copyright (C) 2019 tribe29 GmbH - License: GNU General Public License v2
# This file is part of Checkmk (https://checkmk.com). It is subject to the terms and
# conditions defined in the file COPYING, which is part of this source code package.
"""Currently this module manages the inventory tree which is built
while the inventory is performed for one host.

In the future all inventory code should be moved to this module."""

import os
from contextlib import suppress
from typing import (
    Container,
    Dict,
    List,
    NamedTuple,
    Optional,
    Sequence,
    Tuple,
)

from cmk.utils.check_utils import ActiveCheckResult
import cmk.utils.cleanup
import cmk.utils.debug
import cmk.utils.paths
import cmk.utils.store as store
import cmk.utils.tty as tty
from cmk.utils.exceptions import MKGeneralException, OnError
from cmk.utils.log import console
from cmk.utils.structured_data import StructuredDataNode, load_tree_from, save_tree_to
from cmk.utils.type_defs import (
    EVERYTHING,
    HostAddress,
    HostName,
    HostKey,
    InventoryPluginName,
    result,
    ServiceState,
    SourceType,
    state_markers,
)

from cmk.core_helpers.type_defs import Mode, NO_SELECTION, SectionNameCollection
from cmk.core_helpers.host_sections import HostSections

import cmk.base.api.agent_based.register as agent_based_register
import cmk.base.agent_based.decorator as decorator
from cmk.base.agent_based.data_provider import make_broker, ParsedSectionsBroker
from cmk.base.agent_based.utils import get_section_kwargs, check_sources, check_parsing_errors
import cmk.base.config as config
import cmk.base.section as section
from cmk.base.sources import Source

from .utils import InventoryTrees
from ._tree_aggregator import TreeAggregator


class ActiveInventoryResult(NamedTuple):
    trees: InventoryTrees
    source_results: Sequence[Tuple[Source, result.Result[HostSections, Exception]]]
    parsing_errors: Sequence[str]
    safe_to_write: bool


#   .--cmk -i--------------------------------------------------------------.
#   |                                   _            _                     |
#   |                     ___ _ __ ___ | | __       (_)                    |
#   |                    / __| '_ ` _ \| |/ /  _____| |                    |
#   |                   | (__| | | | | |   <  |_____| |                    |
#   |                    \___|_| |_| |_|_|\_\       |_|                    |
#   |                                                                      |
#   '----------------------------------------------------------------------'


def commandline_inventory(
    hostnames: List[HostName],
    *,
    selected_sections: SectionNameCollection,
    run_plugin_names: Container[InventoryPluginName] = EVERYTHING,
) -> None:
    store.makedirs(cmk.utils.paths.inventory_output_dir)
    store.makedirs(cmk.utils.paths.inventory_archive_dir)

    for hostname in hostnames:
        section.section_begin(hostname)
        try:
            host_config = config.HostConfig.make_host_config(hostname)
            inv_result = _inventorize_host(
                host_config=host_config,
                selected_sections=selected_sections,
                run_plugin_names=run_plugin_names,
            )

            _run_inventory_export_hooks(host_config, inv_result.trees.inventory)
            # TODO: inv_results.source_results is completely ignored here.
            # We should process the results to make errors visible on the console
            _show_inventory_results_on_console(inv_result.trees)

            for detail in check_parsing_errors(errors=inv_result.parsing_errors).details:
                console.warning(detail)

        except Exception as e:
            if cmk.utils.debug.enabled():
                raise

            section.section_error("%s" % e)
        finally:
            cmk.utils.cleanup.cleanup_globals()


def _show_inventory_results_on_console(trees: InventoryTrees) -> None:
    section.section_success("Found %s%s%d%s inventory entries" %
                            (tty.bold, tty.yellow, trees.inventory.count_entries(), tty.normal))
    section.section_success("Found %s%s%d%s status entries" %
                            (tty.bold, tty.yellow, trees.status_data.count_entries(), tty.normal))


#.
#   .--Inventory Check-----------------------------------------------------.
#   |            ___                      _                                |
#   |           |_ _|_ ____   _____ _ __ | |_ ___  _ __ _   _              |
#   |            | || '_ \ \ / / _ \ '_ \| __/ _ \| '__| | | |             |
#   |            | || | | \ V /  __/ | | | || (_) | |  | |_| |             |
#   |           |___|_| |_|\_/ \___|_| |_|\__\___/|_|   \__, |             |
#   |                                                   |___/              |
#   |                      ____ _               _                          |
#   |                     / ___| |__   ___  ___| | __                      |
#   |                    | |   | '_ \ / _ \/ __| |/ /                      |
#   |                    | |___| | | |  __/ (__|   <                       |
#   |                     \____|_| |_|\___|\___|_|\_\                      |
#   |                                                                      |
#   '----------------------------------------------------------------------'


@decorator.handle_check_mk_check_result("check_mk_active-cmk_inv", "Check_MK HW/SW Inventory")
def active_check_inventory(hostname: HostName, options: Dict[str, int]) -> ActiveCheckResult:
    # TODO: drop '_inv_'
    _inv_hw_changes = options.get("hw-changes", 0)
    _inv_sw_changes = options.get("sw-changes", 0)
    _inv_sw_missing = options.get("sw-missing", 0)
    _inv_fail_status = options.get("inv-fail-status", 1)

    host_config = config.HostConfig.make_host_config(hostname)

    inv_result = _inventorize_host(
        host_config=host_config,
        selected_sections=NO_SELECTION,
        run_plugin_names=EVERYTHING,
    )
    trees = inv_result.trees

    if inv_result.safe_to_write:
        old_tree = _save_inventory_tree(hostname, trees.inventory)
        update_result = ActiveCheckResult(0, (), (), ())
    else:
        old_tree, sources_state = None, 1
        update_result = ActiveCheckResult(sources_state,
                                          (f"Cannot update tree{state_markers[sources_state]}",),
                                          (), ())

    _run_inventory_export_hooks(host_config, trees.inventory)

    return ActiveCheckResult.from_subresults(
        update_result,
        _check_inventory_tree(trees, old_tree, _inv_sw_missing, _inv_sw_changes, _inv_hw_changes),
        *check_sources(
            source_results=inv_result.source_results,
            mode=Mode.INVENTORY,
            # Do not use source states which would overwrite "State when inventory fails" in the
            # ruleset "Do hardware/software Inventory". These are handled by the "Check_MK" service
            override_non_ok_state=_inv_fail_status,
        ),
        check_parsing_errors(
            errors=inv_result.parsing_errors,
            error_state=_inv_fail_status,
        ),
    )


def _check_inventory_tree(
    trees: InventoryTrees,
    old_tree: Optional[StructuredDataNode],
    sw_missing: ServiceState,
    sw_changes: ServiceState,
    hw_changes: ServiceState,
) -> ActiveCheckResult:
    if trees.inventory.is_empty() and trees.status_data.is_empty():
        return ActiveCheckResult(0, ("Found no data",), (), ())

    status = 0
    infotexts = [f"Found {trees.inventory.count_entries()} inventory entries"]

    # Node 'software' is always there because _do_inv_for creates this node for cluster info
    sw_container = trees.inventory.get_node(['software'])
    if sw_container is not None and not sw_container.has_edge('packages') and sw_missing:
        infotexts.append("software packages information is missing" + state_markers[sw_missing])
        status = max(status, sw_missing)

    if old_tree is not None:
        if not old_tree.is_equal(trees.inventory, edges=["software"]):
            infotexts.append("software changes" + state_markers[sw_changes])
            status = max(status, sw_changes)

        if not old_tree.is_equal(trees.inventory, edges=["hardware"]):
            infotexts.append("hardware changes" + state_markers[hw_changes])
            status = max(status, hw_changes)

    if not trees.status_data.is_empty():
        infotexts.append(f"Found {trees.status_data.count_entries()} status entries")

    return ActiveCheckResult(status, infotexts, (), ())


def _inventorize_host(
    *,
    host_config: config.HostConfig,
    run_plugin_names: Container[InventoryPluginName],
    selected_sections: SectionNameCollection,
) -> ActiveInventoryResult:
    if host_config.is_cluster:
        return ActiveInventoryResult(
            trees=_do_inv_for_cluster(host_config),
            source_results=(),
            parsing_errors=(),
            safe_to_write=True,
        )

    ipaddress = config.lookup_ip_address(host_config)
    config_cache = config.get_config_cache()

    broker, results = make_broker(
        config_cache=config_cache,
        host_config=host_config,
        ip_address=ipaddress,
        selected_sections=selected_sections,
        mode=(Mode.INVENTORY if selected_sections is NO_SELECTION else Mode.FORCE_SECTIONS),
        file_cache_max_age=host_config.max_cachefile_age,
        fetcher_messages=(),
        force_snmp_cache_refresh=False,
        on_scan_error=OnError.RAISE,
    )

    parsing_errors = broker.parsing_errors()
    return ActiveInventoryResult(
        trees=_do_inv_for_realhost(
            host_config,
            ipaddress,
            parsed_sections_broker=broker,
            run_plugin_names=run_plugin_names,
        ),
        source_results=results,
        parsing_errors=parsing_errors,
        safe_to_write=(
            _safe_to_write_tree(results) and  #
            selected_sections is NO_SELECTION and  #
            run_plugin_names is EVERYTHING and  #
            not parsing_errors),
    )


def _safe_to_write_tree(
    results: Sequence[Tuple[Source, result.Result[HostSections, Exception]]],) -> bool:
    """Check if data sources of a host failed

    If a data source failed, we may have incomlete data. In that case we
    may not write it to disk because that would result in a flapping state
    of the tree, which would blow up the inventory history (in terms of disk usage).
    """
    # If a result is not OK, that means the corresponding sections have not been added.
    return all(source_result.is_ok() for _source, source_result in results)


def do_inventory_actions_during_checking_for(
    config_cache: config.ConfigCache,
    host_config: config.HostConfig,
    ipaddress: Optional[HostAddress],
    *,
    parsed_sections_broker: ParsedSectionsBroker,
) -> None:

    if not host_config.do_status_data_inventory:
        # includes cluster case
        _cleanup_status_data(host_config.hostname)
        return  # nothing to do here

    trees = _do_inv_for_realhost(
        host_config,
        ipaddress,
        parsed_sections_broker=parsed_sections_broker,
        run_plugin_names=EVERYTHING,
    )
    _save_status_data_tree(host_config.hostname, trees.status_data)


def _cleanup_status_data(hostname: HostName) -> None:
    """Remove empty status data files"""
    filepath = "%s/%s" % (cmk.utils.paths.status_data_dir, hostname)
    with suppress(OSError):
        os.remove(filepath)
    with suppress(OSError):
        os.remove(filepath + ".gz")


def _do_inv_for_cluster(host_config: config.HostConfig) -> InventoryTrees:
    inventory_tree = StructuredDataNode()
    _set_cluster_property(inventory_tree, host_config)

    if not host_config.nodes:
        return InventoryTrees(inventory_tree, StructuredDataNode())

    inv_node = inventory_tree.get_list("software.applications.check_mk.cluster.nodes:")
    for node_name in host_config.nodes:
        inv_node.append({
            "name": node_name,
        })

    inventory_tree.normalize_nodes()
    return InventoryTrees(inventory_tree, StructuredDataNode())


def _do_inv_for_realhost(
    host_config: config.HostConfig,
    ipaddress: Optional[HostAddress],
    *,
    parsed_sections_broker: ParsedSectionsBroker,
    run_plugin_names: Container[InventoryPluginName],
) -> InventoryTrees:
    tree_aggregator = TreeAggregator()
    _set_cluster_property(tree_aggregator.trees.inventory, host_config)

    section.section_step("Executing inventory plugins")
    for inventory_plugin in agent_based_register.iter_all_inventory_plugins():
        if inventory_plugin.name not in run_plugin_names:
            continue

        for source_type in (SourceType.HOST, SourceType.MANAGEMENT):
            kwargs = get_section_kwargs(
                parsed_sections_broker,
                HostKey(host_config.hostname, ipaddress, source_type),
                inventory_plugin.sections,
            )
            if not kwargs:
                console.vverbose(" %s%s%s%s: skipped (no data)\n", tty.yellow, tty.bold,
                                 inventory_plugin.name, tty.normal)
                continue

            # Inventory functions can optionally have a second argument: parameters.
            # These are configured via rule sets (much like check parameters).
            if inventory_plugin.inventory_ruleset_name is not None:
                kwargs["params"] = host_config.inventory_parameters(
                    inventory_plugin.inventory_ruleset_name)

            exception = tree_aggregator.aggregate_results(
                inventory_plugin.inventory_function(**kwargs),)
            if exception:
                console.warning(" %s%s%s%s: failed: %s", tty.red, tty.bold, inventory_plugin.name,
                                tty.normal, exception)
            else:
                console.verbose(" %s%s%s%s", tty.green, tty.bold, inventory_plugin.name, tty.normal)
                console.vverbose(": ok\n")
    console.verbose("\n")

    tree_aggregator.trees.inventory.normalize_nodes()
    tree_aggregator.trees.status_data.normalize_nodes()
    return tree_aggregator.trees


def _set_cluster_property(
    inventory_tree: StructuredDataNode,
    host_config: config.HostConfig,
) -> None:
    inventory_tree.get_dict(
        "software.applications.check_mk.cluster.")["is_cluster"] = host_config.is_cluster


def _save_inventory_tree(
    hostname: HostName,
    inventory_tree: StructuredDataNode,
) -> Optional[StructuredDataNode]:
    store.makedirs(cmk.utils.paths.inventory_output_dir)

    filepath = cmk.utils.paths.inventory_output_dir + "/" + hostname
    if inventory_tree.is_empty():
        # Remove empty inventory files. Important for host inventory icon
        if os.path.exists(filepath):
            os.remove(filepath)
        if os.path.exists(filepath + ".gz"):
            os.remove(filepath + ".gz")
        return None

    old_tree = load_tree_from(filepath)
    old_tree.normalize_nodes()
    if old_tree.is_equal(inventory_tree):
        console.verbose("Inventory was unchanged\n")
        return None

    if old_tree.is_empty():
        console.verbose("New inventory tree\n")
    else:
        console.verbose("Inventory tree has changed\n")
        old_time = os.stat(filepath).st_mtime
        arcdir = "%s/%s" % (cmk.utils.paths.inventory_archive_dir, hostname)
        store.makedirs(arcdir)
        os.rename(filepath, arcdir + ("/%d" % old_time))
    save_tree_to(inventory_tree, cmk.utils.paths.inventory_output_dir, hostname)
    return old_tree


def _save_status_data_tree(hostname: HostName, status_data_tree: StructuredDataNode) -> None:
    if status_data_tree and not status_data_tree.is_empty():
        store.makedirs(cmk.utils.paths.status_data_dir)
        save_tree_to(status_data_tree, cmk.utils.paths.status_data_dir, hostname)


def _run_inventory_export_hooks(host_config: config.HostConfig,
                                inventory_tree: StructuredDataNode) -> None:
    import cmk.base.inventory_plugins as inventory_plugins  # pylint: disable=import-outside-toplevel
    hooks = host_config.inventory_export_hooks

    if not hooks:
        return

    section.section_step("Execute inventory export hooks")
    for hookname, params in hooks:
        console.verbose("Execute export hook: %s%s%s%s" %
                        (tty.blue, tty.bold, hookname, tty.normal))
        try:
            func = inventory_plugins.inv_export[hookname]["export_function"]
            func(host_config.hostname, params, inventory_tree.get_raw_tree())
        except Exception as e:
            if cmk.utils.debug.enabled():
                raise
            raise MKGeneralException("Failed to execute export hook %s: %s" % (hookname, e))
