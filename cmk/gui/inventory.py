#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# Copyright (C) 2019 tribe29 GmbH - License: GNU General Public License v2
# This file is part of Checkmk (https://checkmk.com). It is subject to the terms and
# conditions defined in the file COPYING, which is part of this source code package.

import ast
import json
import os
import shutil
import time
import xml.dom.minidom  # type: ignore[import]
from typing import Any, Dict, List, Literal, Optional, Tuple, Union
from pathlib import Path

import dicttoxml  # type: ignore[import]

import livestatus

import cmk.utils.paths
from cmk.utils.structured_data import StructuredDataNode, load_tree_from
from cmk.utils.exceptions import (
    MKException,
    MKGeneralException,
)
import cmk.utils.store as store
from cmk.utils.type_defs import HostName

from cmk.gui.type_defs import Row
import cmk.gui.pages
import cmk.gui.config as config
import cmk.gui.userdb as userdb
import cmk.gui.sites as sites
from cmk.gui.i18n import _
from cmk.gui.globals import g, html, request as request, response
from cmk.gui.exceptions import (
    MKAuthException,
    MKUserError,
)
from cmk.gui.valuespec import ValueSpec, TextInput

# TODO Cleanup variation:
#   - parse_tree_path parses NOT visible, internal tree paths used in displayhints/views
#   - _parse_visible_raw_inventory_path parses visible, internal tree paths for contact groups etc.
# => Should be unified one day.

RawInventoryPath = str
InventoryPath = List[Union[str, int]]
# TODO Cleanup
AttributesKeys = List[str]
# TODO: Can we be more specific on the return value type?
InventoryData = Any
InventoryDeltaData = Tuple[int, int, int, StructuredDataNode]


def get_inventory_data(inventory_tree: StructuredDataNode,
                       tree_path: RawInventoryPath) -> InventoryData:
    invdata = None
    parsed_path, attribute_keys = parse_tree_path(tree_path)
    if attribute_keys == []:
        table = inventory_tree.get_table(parsed_path)
        if table is not None:
            invdata = table.data
    elif attribute_keys:
        attributes = inventory_tree.get_attributes(parsed_path)
        if attributes is not None:
            # In paint_host_inventory_tree we parse invpath and get
            # a path and attribute_keys which may be either None, [], or ["KEY"].
            invdata = attributes.data.get(attribute_keys[-1])
    return invdata


def parse_tree_path(raw_path: RawInventoryPath) -> Tuple[InventoryPath, Optional[AttributesKeys]]:
    # raw_path may look like:
    # .                          (ROOT) => path = []                            key = None
    # .hardware.                 (dict) => path = ["hardware"],                 key = None
    # .hardware.cpu.model        (leaf) => path = ["hardware", "cpu"],          key = "model"
    # .hardware.cpu.             (dict) => path = ["hardware", "cpu"],          key = None
    # .software.packages:17.name (leaf) => path = ["software", "packages", 17], key = "name"
    # .software.packages:        (list) => path = ["software", "packages"],     key = []
    if not raw_path:
        return [], None

    path: List[str]
    attribute_keys: Optional[AttributesKeys]

    if raw_path.endswith(":"):
        path = raw_path[:-1].strip(".").split(".")
        attribute_keys = []
    elif raw_path.endswith("."):
        path = raw_path[:-1].strip(".").split(".")
        attribute_keys = None
    else:
        path = raw_path.strip(".").split(".")
        attribute_keys = [path.pop(-1)]

    parsed_path: InventoryPath = []
    for part in path:
        if ":" in part:
            # Nested tables, see also lib/structured_data.py
            parts = part.split(":")
        else:
            parts = [part]

        for part_ in parts:
            if not part_:
                continue
            try:
                parsed_path.append(int(part_))
            except ValueError:
                parsed_path.append(part_)
    return parsed_path, attribute_keys


def load_filtered_inventory_tree(hostname: Optional[HostName]) -> Optional[StructuredDataNode]:
    """Loads the host inventory tree from the current file and returns the filtered tree"""
    return _filter_tree(_load_structured_data_tree("inventory", hostname))


def load_filtered_and_merged_tree(row: Row) -> Optional[StructuredDataNode]:
    """Load inventory tree from file, status data tree from row,
    merge these trees and returns the filtered tree"""
    hostname = row.get("host_name")
    inventory_tree = _load_structured_data_tree("inventory", hostname)
    status_data_tree = _create_tree_from_raw_tree(row.get("host_structured_status"))
    # If no data from livestatus could be fetched (CRE) try to load from cache
    # or status dir
    if status_data_tree is None:
        status_data_tree = _load_structured_data_tree("status_data", hostname)

    merged_tree = _merge_inventory_and_status_data_tree(inventory_tree, status_data_tree)
    return _filter_tree(merged_tree)


def get_status_data_via_livestatus(site: Optional[livestatus.SiteId], hostname: HostName) -> Row:
    query = "GET hosts\nColumns: host_structured_status\nFilter: host_name = %s\n" % livestatus.lqencode(
        hostname)
    try:
        sites.live().set_only_sites([site] if site else None)
        result = sites.live().query(query)
    finally:
        sites.live().set_only_sites()

    row = {"host_name": hostname}
    if result and result[0]:
        row["host_structured_status"] = result[0][0]
    return row


def load_delta_tree(hostname: HostName,
                    timestamp: int) -> Tuple[Optional[StructuredDataNode], List[str]]:
    """Load inventory history and compute delta tree of a specific timestamp"""
    # Timestamp is timestamp of the younger of both trees. For the oldest
    # tree we will just return the complete tree - without any delta
    # computation.
    delta_history, corrupted_history_files = \
        get_history_deltas(hostname, search_timestamp=str(timestamp))
    if not delta_history:
        return None, []
    return delta_history[0][1][3], corrupted_history_files


def get_history_deltas(
    hostname: HostName,
    search_timestamp: Optional[str] = None
) -> Tuple[List[Tuple[str, InventoryDeltaData]], List[str]]:
    if '/' in hostname:
        return [], []  # just for security reasons

    inventory_path = "%s/%s" % (cmk.utils.paths.inventory_output_dir, hostname)
    if not os.path.exists(inventory_path):
        return [], []

    latest_timestamp = str(int(os.stat(inventory_path).st_mtime))
    inventory_archive_dir = "%s/%s" % (cmk.utils.paths.inventory_archive_dir, hostname)
    try:
        archived_timestamps = sorted(os.listdir(inventory_archive_dir))
    except OSError:
        return [], []

    all_timestamps: List[str] = archived_timestamps + [latest_timestamp]
    previous_timestamp: Optional[str] = None

    if not search_timestamp:
        required_timestamps = all_timestamps
    else:
        new_timestamp_idx = all_timestamps.index(search_timestamp)
        if new_timestamp_idx == 0:
            required_timestamps = [search_timestamp]
        else:
            previous_timestamp = all_timestamps[new_timestamp_idx - 1]
            required_timestamps = [search_timestamp]

    tree_lookup: Dict[str, Any] = {}

    def get_tree(timestamp: Optional[str]) -> StructuredDataNode:
        if timestamp is None:
            return StructuredDataNode()

        if timestamp in tree_lookup:
            return tree_lookup[timestamp]

        if timestamp == latest_timestamp:
            inventory_tree = load_filtered_inventory_tree(hostname)
            if inventory_tree is None:
                raise LoadStructuredDataError()
            tree_lookup[timestamp] = inventory_tree
        else:
            inventory_archive_path = "%s/%s" % (inventory_archive_dir, timestamp)
            tree_lookup[timestamp] = _filter_tree(load_tree_from(inventory_archive_path))
        return tree_lookup[timestamp]

    corrupted_history_files = []
    delta_history: List[Tuple[str, InventoryDeltaData]] = []
    for _idx, timestamp in enumerate(required_timestamps):
        cached_delta_path = os.path.join(cmk.utils.paths.var_dir, "inventory_delta_cache", hostname,
                                         "%s_%s" % (previous_timestamp, timestamp))

        cached_data = None
        try:
            cached_data = store.load_object_from_file(cached_delta_path)
        except MKGeneralException:
            pass

        if cached_data:
            new, changed, removed, delta_tree_data = cached_data
            delta_tree = StructuredDataNode()
            delta_tree.create_tree_from_raw_tree(delta_tree_data)
            delta_history.append((timestamp, (new, changed, removed, delta_tree)))
            previous_timestamp = timestamp
            continue

        try:
            previous_tree = get_tree(previous_timestamp)
            current_tree = get_tree(timestamp)
            delta_result = current_tree.compare_with(previous_tree)
            delta_data = (delta_result.counter['new'], delta_result.counter['changed'],
                          delta_result.counter['removed'], delta_result.delta)
            new, changed, removed, delta_tree = delta_data
            if new or changed or removed:
                store.save_file(
                    cached_delta_path,
                    repr((new, changed, removed, delta_tree.get_raw_tree())),
                )
                delta_history.append((timestamp, delta_data))
        except LoadStructuredDataError:
            corrupted_history_files.append(
                str(get_short_inventory_history_filepath(hostname, timestamp)))

        previous_timestamp = timestamp

    return delta_history, corrupted_history_files


def get_short_inventory_filepath(hostname: HostName) -> Path:
    return Path(cmk.utils.paths.inventory_output_dir).joinpath(hostname).relative_to(
        cmk.utils.paths.omd_root)


def get_short_inventory_history_filepath(hostname: HostName, timestamp: str) -> Path:
    return Path(cmk.utils.paths.inventory_archive_dir).joinpath(
        "%s/%s" % (hostname, timestamp)).relative_to(cmk.utils.paths.omd_root)


def parent_path(invpath: RawInventoryPath) -> Optional[RawInventoryPath]:
    """Gets the parent path by dropping the last component"""
    if invpath == ".":
        return None  # No parent

    if invpath[-1] in ".:":  # drop trailing type specifyer
        invpath = invpath[:-1]

    last_sep = max(invpath.rfind(":"), invpath.rfind("."))
    return invpath[:last_sep + 1]


def vs_inventory_path() -> ValueSpec:
    # Via 'Display options::Show internal tree paths' the tree paths are shown as 'path.to.node'.
    # We keep this format in order to easily copy&paste these tree paths to
    # 'Contact groups::Permitted HW/SW inventory paths'.
    return TextInput(
        title=_("Path to attributes or tables"),
        size=60,
        allow_empty=False,
        help=_("Via <tt>Display options > Show internal tree paths</tt>"
               " on the HW/SW Inventory page of a host you can make the"
               " internal tree paths visible which can be inserted here."),
    )


def _parse_visible_raw_inventory_path(raw_path: str) -> InventoryPath:
    parsed: InventoryPath = []
    for part in raw_path.split("."):
        if not part:
            continue
        try:
            parsed.append(int(part))
        except ValueError:
            parsed.append(part)
    return parsed


#.
#   .--helpers-------------------------------------------------------------.
#   |                  _          _                                        |
#   |                 | |__   ___| |_ __   ___ _ __ ___                    |
#   |                 | '_ \ / _ \ | '_ \ / _ \ '__/ __|                   |
#   |                 | | | |  __/ | |_) |  __/ |  \__ \                   |
#   |                 |_| |_|\___|_| .__/ \___|_|  |___/                   |
#   |                              |_|                                     |
#   '----------------------------------------------------------------------'


class LoadStructuredDataError(MKException):
    pass


def _load_structured_data_tree(tree_type: Literal["inventory", "status_data"],
                               hostname: Optional[HostName]) -> Optional[StructuredDataNode]:
    """Load data of a host, cache it in the current HTTP request"""
    if not hostname:
        return None

    inventory_tree_cache = g.setdefault(tree_type, {})
    if hostname in inventory_tree_cache:
        inventory_tree = inventory_tree_cache[hostname]
    else:
        if '/' in hostname:
            # just for security reasons
            return None
        cache_path = "%s/%s" % (cmk.utils.paths.inventory_output_dir if tree_type == "inventory"
                                else cmk.utils.paths.status_data_dir, hostname)
        try:
            inventory_tree = load_tree_from(cache_path)
        except Exception as e:
            if config.debug:
                html.show_warning("%s" % e)
            raise LoadStructuredDataError()
        inventory_tree_cache[hostname] = inventory_tree
    return inventory_tree


def _create_tree_from_raw_tree(raw_tree: Optional[bytes]) -> Optional[StructuredDataNode]:
    if raw_tree:
        return StructuredDataNode().create_tree_from_raw_tree(
            ast.literal_eval(raw_tree.decode("utf-8")))
    return None


def _merge_inventory_and_status_data_tree(inventory_tree, status_data_tree):
    if inventory_tree is None and status_data_tree is None:
        return

    if inventory_tree is None:
        inventory_tree = StructuredDataNode()

    if status_data_tree is not None:
        inventory_tree.merge_with(status_data_tree)
    return inventory_tree


def _filter_tree(struct_tree: Optional[StructuredDataNode]) -> Optional[StructuredDataNode]:
    if struct_tree is None:
        return None
    return struct_tree.get_filtered_node(_get_permitted_inventory_paths())


def _get_permitted_inventory_paths():
    """
    Returns either a list of permitted paths or
    None in case the user is allowed to see the whole tree.
    """
    if 'permitted_inventory_paths' in g:
        return g.permitted_inventory_paths

    user_groups = [] if config.user.id is None else userdb.contactgroups_of_user(config.user.id)

    if not user_groups:
        g.permitted_inventory_paths = None
        return None

    forbid_whole_tree = False
    permitted_paths = []
    for user_group in user_groups:
        inventory_paths = config.multisite_contactgroups.get(user_group, {}).get('inventory_paths')
        if inventory_paths is None:
            # Old configuration: no paths configured means 'allow_all'
            g.permitted_inventory_paths = None
            return None

        if inventory_paths == "allow_all":
            g.permitted_inventory_paths = None
            return None

        if inventory_paths == "forbid_all":
            forbid_whole_tree = True
            continue

        for entry in inventory_paths[1]:
            permitted_paths.append(
                (_parse_visible_raw_inventory_path(entry["path"]), entry.get("attributes")))

    if forbid_whole_tree and not permitted_paths:
        g.permitted_inventory_paths = []
        return []

    g.permitted_inventory_paths = permitted_paths
    return permitted_paths


#.
#   .--Inventory API-------------------------------------------------------.
#   |   ___                      _                        _    ____ ___    |
#   |  |_ _|_ ____   _____ _ __ | |_ ___  _ __ _   _     / \  |  _ \_ _|   |
#   |   | || '_ \ \ / / _ \ '_ \| __/ _ \| '__| | | |   / _ \ | |_) | |    |
#   |   | || | | \ V /  __/ | | | || (_) | |  | |_| |  / ___ \|  __/| |    |
#   |  |___|_| |_|\_/ \___|_| |_|\__\___/|_|   \__, | /_/   \_\_|  |___|   |
#   |                                          |___/                       |
#   '----------------------------------------------------------------------'


@cmk.gui.pages.register("host_inv_api")
def page_host_inv_api() -> None:
    # The response is always a top level dict with two elements:
    # a) result_code - This is 0 for expected processing and 1 for an error
    # b) result      - In case of an error this is the error message, a UTF-8 encoded string.
    #                  In case of success this is a dictionary containing the host inventory.
    try:
        api_request = request.get_request()
        # The user can either specify a single host or provide a list of host names. In case
        # multiple hosts are handled, there is a top level dict added with "host > invdict" pairs
        hosts = api_request.get("hosts")
        if hosts:
            result = {}
            for a_host_name in hosts:
                result[a_host_name] = inventory_of_host(a_host_name, api_request)

        else:
            host_name = api_request.get("host")
            if host_name is None:
                raise MKUserError("host", _("You need to provide a \"host\"."))

            result = inventory_of_host(host_name, api_request)

            if not result and not has_inventory(host_name):
                raise MKGeneralException(_("Found no inventory data for this host."))

        resp = {"result_code": 0, "result": result}

    except MKException as e:
        resp = {"result_code": 1, "result": "%s" % e}

    except Exception as e:
        if config.debug:
            raise
        resp = {"result_code": 1, "result": "%s" % e}

    if html.output_format == "json":
        _write_json(resp)
    elif html.output_format == "xml":
        _write_xml(resp)
    else:
        _write_python(resp)


def has_inventory(hostname):
    if not hostname:
        return False
    inventory_path = "%s/%s" % (cmk.utils.paths.inventory_output_dir, hostname)
    return os.path.exists(inventory_path)


def inventory_of_host(host_name: HostName, api_request):
    raw_site = api_request.get("site")
    site = livestatus.SiteId(raw_site) if raw_site is not None else None
    verify_permission(host_name, site)

    row = get_status_data_via_livestatus(site, host_name)
    merged_tree = load_filtered_and_merged_tree(row)
    if not merged_tree:
        return {}

    if "paths" in api_request:
        parsed_paths = []
        for path in api_request["paths"]:
            parsed_paths.append(parse_tree_path(path))
        merged_tree = merged_tree.get_filtered_node(parsed_paths)

    assert merged_tree is not None
    return merged_tree.get_raw_tree()


def verify_permission(host_name: HostName, site: Optional[livestatus.SiteId]) -> None:
    if config.user.may("general.see_all"):
        return

    query = "GET hosts\nFilter: host_name = %s\nStats: state >= 0%s" % (
        livestatus.lqencode(host_name),
        "\nAuthUser: %s" % livestatus.lqencode(config.user.id) if config.user.id else "",
    )

    if site:
        sites.live().set_only_sites([site])

    try:
        result = sites.live().query_summed_stats(query, "ColumnHeaders: off\n")
    except livestatus.MKLivestatusNotFoundError:
        raise MKAuthException(
            _("No such inventory tree of host %s."
              " You may also have no access to this host.") % host_name)
    finally:
        if site:
            sites.live().set_only_sites()

    if result[0] == 0:
        raise MKAuthException(_("You are not allowed to access the host %s.") % host_name)


def _write_xml(resp):
    unformated_xml = dicttoxml.dicttoxml(resp)
    dom = xml.dom.minidom.parseString(unformated_xml)
    response.set_data(dom.toprettyxml())


def _write_json(resp):
    response.set_data(json.dumps(resp, sort_keys=True, indent=4, separators=(',', ': ')))


def _write_python(resp):
    response.set_data(repr(resp))


class InventoryHousekeeping:
    def __init__(self):
        super(InventoryHousekeeping, self).__init__()
        self._inventory_path = Path(cmk.utils.paths.inventory_output_dir)
        self._inventory_archive_path = Path(cmk.utils.paths.inventory_archive_dir)
        self._inventory_delta_cache_path = Path(cmk.utils.paths.var_dir) / "inventory_delta_cache"

    def run(self):
        if not self._inventory_delta_cache_path.exists() or not self._inventory_archive_path.exists(
        ):
            return

        last_cleanup = self._inventory_delta_cache_path / "last_cleanup"
        # TODO: remove with pylint 2
        if last_cleanup.exists() and time.time() - last_cleanup.stat().st_mtime < 3600 * 12:
            return

        # TODO: remove with pylint 2
        inventory_archive_hosts = {
            x.name for x in self._inventory_archive_path.iterdir() if x.is_dir()
        }
        inventory_delta_cache_hosts = {
            x.name for x in self._inventory_delta_cache_path.iterdir() if x.is_dir()
        }

        folders_to_delete = inventory_delta_cache_hosts - inventory_archive_hosts
        for foldername in folders_to_delete:
            shutil.rmtree(str(self._inventory_delta_cache_path / foldername))

        inventory_delta_cache_hosts -= folders_to_delete
        for hostname in inventory_delta_cache_hosts:
            available_timestamps = self._get_timestamps_for_host(hostname)
            for filename in [
                    x.name
                    for x in (self._inventory_delta_cache_path / hostname).iterdir()
                    if not x.is_dir()
            ]:
                delete = False
                try:
                    first, second = filename.split("_")
                    if first not in available_timestamps or second not in available_timestamps:
                        delete = True
                except ValueError:
                    delete = True
                if delete:
                    (self._inventory_delta_cache_path / hostname / filename).unlink()

        # TODO: remove with pylint 2
        last_cleanup.touch()

    def _get_timestamps_for_host(self, hostname):
        timestamps = {"None"}  # 'None' refers to the histories start
        try:
            timestamps.add("%d" % (self._inventory_path / hostname).stat().st_mtime)
        except OSError:
            pass

        for filename in [
                x for x in (self._inventory_archive_path / hostname).iterdir() if not x.is_dir()
        ]:
            timestamps.add(filename.name)
        return timestamps
