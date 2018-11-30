WEBINJECT := Webinject
WEBINJECT_VERS := 1.88
WEBINJECT_DIR := Webinject-$(WEBINJECT_VERS)


WEBINJECT_BUILD := $(BUILD_HELPER_DIR)/$(WEBINJECT_DIR)-build
WEBINJECT_INSTALL := $(BUILD_HELPER_DIR)/$(WEBINJECT_DIR)-install
WEBINJECT_PATCHING := $(BUILD_HELPER_DIR)/$(WEBINJECT_DIR)-patching

.PHONY: $(WEBINJECT) $(WEBINJECT)-install $(WEBINJECT)-skel $(WEBINJECT)-build

$(WEBINJECT): $(WEBINJECT_BUILD)

$(WEBINJECT)-install: $(WEBINJECT_INSTALL)

$(WEBINJECT_BUILD): $(WEBINJECT_PATCHING) $(PERL_MODULES_BUILD)
	export PERL5LIB=$(PACKAGE_PERL_MODULES_PERL5LIB); \
	    cd $(WEBINJECT_DIR) && echo "" | $(PERL) Makefile.PL
	cd $(WEBINJECT_DIR) && $(MAKE) check_webinject
	$(TOUCH) $@

$(WEBINJECT_INSTALL): $(WEBINJECT_BUILD)
	install -m 755 $(WEBINJECT_DIR)/check_webinject $(DESTDIR)$(OMD_ROOT)/lib/nagios/plugins/
	$(TOUCH) $@

$(WEBINJECT)-skel:

$(WEBINJECT)-clean:
	$(RM) -r $(WEBINJECT_DIR) $(BUILD_HELPER_DIR)/$(WEBINJECT_DIR)*
