"""Tests for 'show configuration' (operational) and 'show <section>' (configure)."""
from __future__ import annotations

import pytest
from prompt_toolkit.completion import CompleteEvent
from prompt_toolkit.document import Document

from nos.cli.completer import NOSCompleter
from nos.cli.modes.configure import ConfigureMode
from nos.cli.modes.operational import OperationalMode
from nos.cli.parser import CLIMode
from nos.config.commit import CommitEngine
from nos.config.store import ConfigStore
from nos.config.validator import ConfigValidator


# ============================================================================
# Fixtures
# ============================================================================

@pytest.fixture
def store(tmp_path):
    return ConfigStore(base_dir=tmp_path)


@pytest.fixture
def engine(store):
    return CommitEngine(store, validator=ConfigValidator())


@pytest.fixture
def oper(store):
    return OperationalMode(store)


@pytest.fixture
def conf(store, engine):
    return ConfigureMode(store, engine)


@pytest.fixture
def populated_store(store, engine):
    """A store with a representative running config committed."""
    cm = ConfigureMode(store, engine)
    cm.execute("set system host-name nos01")
    cm.execute("set system domain-name example.com")
    cm.execute("set interfaces eth0 description internet")
    cm.execute("set interfaces eth0 family inet address 10.0.0.1/30")
    cm.execute("set interfaces lo0 family inet address 1.1.1.1/32")
    cm.execute("set vlans vlan100 vlan-id 100")
    cm.execute("set vlans vlan100 description management")
    cm.execute("set routing-options router-id 1.1.1.1")
    cm.execute("set routing-options autonomous-system 65000")
    cm.execute("set routing-options static route 0.0.0.0/0 next-hop 10.0.0.2")
    cm.execute("set protocols bgp group IBGP type internal")
    cm.execute("set protocols bgp group IBGP local-address 1.1.1.1")
    cm.execute("set protocols isis interface eth0 point-to-point")
    engine.commit()
    return store


# ============================================================================
# show configuration — handler (operational mode)
# ============================================================================

class TestShowConfigurationHandler:
    def test_empty_config_returns_empty_message(self, oper):
        out = oper.execute("show configuration")
        assert "empty" in out.lower()

    def test_full_config_as_set_commands(self, oper, populated_store):
        out = oper.execute("show configuration")
        assert out.startswith("set ")
        lines = out.splitlines()
        assert all(ln.startswith("set ") for ln in lines)

    def test_full_config_contains_system(self, oper, populated_store):
        out = oper.execute("show configuration")
        assert "set system host-name" in out
        assert "nos01" in out

    def test_full_config_contains_interfaces(self, oper, populated_store):
        out = oper.execute("show configuration")
        assert "set interfaces" in out

    def test_full_config_contains_routing_options(self, oper, populated_store):
        out = oper.execute("show configuration")
        assert "set routing-options" in out

    def test_section_interfaces(self, oper, populated_store):
        out = oper.execute("show configuration interfaces")
        lines = out.splitlines()
        assert lines, "Expected at least one line"
        assert all("interfaces" in ln for ln in lines)
        # Must not contain other sections
        assert not any(ln.startswith("set system") for ln in lines)
        assert not any(ln.startswith("set routing-options") for ln in lines)

    def test_section_system(self, oper, populated_store):
        out = oper.execute("show configuration system")
        lines = out.splitlines()
        assert all(ln.startswith("set system") for ln in lines)
        assert any("host-name" in ln for ln in lines)

    def test_section_routing_options(self, oper, populated_store):
        out = oper.execute("show configuration routing-options")
        lines = out.splitlines()
        assert all(ln.startswith("set routing-options") for ln in lines)

    def test_section_vlans(self, oper, populated_store):
        out = oper.execute("show configuration vlans")
        lines = out.splitlines()
        assert all(ln.startswith("set vlans") for ln in lines)

    def test_section_protocols(self, oper, populated_store):
        out = oper.execute("show configuration protocols")
        lines = out.splitlines()
        assert all(ln.startswith("set protocols") for ln in lines)

    def test_subsection_protocols_bgp(self, oper, populated_store):
        out = oper.execute("show configuration protocols bgp")
        lines = out.splitlines()
        assert lines
        assert all(ln.startswith("set protocols bgp") for ln in lines)
        assert not any("isis" in ln for ln in lines)

    def test_subsection_protocols_isis(self, oper, populated_store):
        out = oper.execute("show configuration protocols isis")
        lines = out.splitlines()
        assert lines
        assert all(ln.startswith("set protocols isis") for ln in lines)
        assert not any("bgp" in ln for ln in lines)

    def test_nonexistent_section(self, oper, populated_store):
        out = oper.execute("show configuration firewall")
        assert "no configuration" in out.lower() or "empty" in out.lower()

    def test_output_is_sorted(self, oper, populated_store):
        out = oper.execute("show configuration")
        lines = out.splitlines()
        assert lines == sorted(lines)

    def test_pipe_match_filters_lines(self, oper, populated_store):
        out = oper.execute("show configuration | match host-name")
        assert "host-name" in out
        lines = out.splitlines()
        assert all("host-name" in ln for ln in lines)

    def test_pipe_except_excludes_lines(self, oper, populated_store):
        out = oper.execute("show configuration | except system")
        assert not any(ln.startswith("set system") for ln in out.splitlines())

    def test_section_with_pipe(self, oper, populated_store):
        out = oper.execute("show configuration interfaces | match eth0")
        lines = out.splitlines()
        assert all("eth0" in ln for ln in lines)


# ============================================================================
# show <section> — handler (configure mode)
# ============================================================================

class TestShowSectionConfigureHandler:
    def _setup(self, conf, engine):
        """Populate candidate config."""
        conf.execute("set system host-name nos01")
        conf.execute("set interfaces eth0 description internet")
        conf.execute("set interfaces eth0 family inet address 10.0.0.1/30")
        conf.execute("set vlans vlan100 vlan-id 100")
        conf.execute("set routing-options router-id 1.1.1.1")
        conf.execute("set protocols bgp group IBGP type internal")
        conf.execute("set protocols bgp group IBGP local-address 1.1.1.1")

    def test_show_no_args_shows_full_candidate(self, conf, engine):
        self._setup(conf, engine)
        out = conf.execute("show")
        assert "interfaces" in out
        assert "system" in out

    def test_show_interfaces_section(self, conf, engine):
        self._setup(conf, engine)
        out = conf.execute("show interfaces")
        # Should contain interface config but NOT system or vlans at top level
        assert "eth0" in out
        assert "description" in out
        assert "host-name" not in out
        assert "vlan-id" not in out

    def test_show_system_section(self, conf, engine):
        self._setup(conf, engine)
        out = conf.execute("show system")
        assert "host-name" in out
        assert "nos01" in out
        assert "interfaces" not in out

    def test_show_vlans_section(self, conf, engine):
        self._setup(conf, engine)
        out = conf.execute("show vlans")
        assert "vlan100" in out
        assert "vlan-id" in out
        assert "host-name" not in out

    def test_show_protocols_section(self, conf, engine):
        self._setup(conf, engine)
        out = conf.execute("show protocols")
        assert "bgp" in out
        assert "IBGP" in out
        assert "host-name" not in out

    def test_show_protocols_bgp_subsection(self, conf, engine):
        self._setup(conf, engine)
        out = conf.execute("show protocols bgp")
        assert "IBGP" in out
        assert "type" in out
        assert "isis" not in out

    def test_show_routing_options_section(self, conf, engine):
        self._setup(conf, engine)
        out = conf.execute("show routing-options")
        assert "router-id" in out

    def test_show_interfaces_eth0_subsection(self, conf, engine):
        self._setup(conf, engine)
        out = conf.execute("show interfaces eth0")
        assert "description" in out
        assert "internet" in out
        assert "family" in out

    def test_show_nonexistent_section(self, conf, engine):
        self._setup(conf, engine)
        out = conf.execute("show firewall")
        assert "no configuration" in out.lower() or "empty" in out.lower()

    def test_show_section_respects_edit_path(self, conf, engine):
        """At (interfaces)#, 'show eth0' shows eth0 config."""
        self._setup(conf, engine)
        conf.edit_path = ["interfaces"]
        out = conf.execute("show eth0")
        assert "description" in out
        assert "internet" in out
        # Other top-level sections should not appear
        assert "host-name" not in out

    def test_show_no_args_respects_edit_path(self, conf, engine):
        """At (protocols bgp)#, bare 'show' shows BGP config."""
        self._setup(conf, engine)
        conf.edit_path = ["protocols", "bgp"]
        out = conf.execute("show")
        assert "IBGP" in out
        assert "type" in out
        # Should not show system or interfaces
        assert "host-name" not in out

    def test_show_compare_still_works(self, conf, engine):
        conf.execute("set system host-name nos01")
        engine.commit()
        conf.execute("set system host-name nos02")
        out = conf.execute("show | compare")
        assert "nos02" in out or "nos01" in out

    def test_show_section_pipe_match(self, conf, engine):
        self._setup(conf, engine)
        out = conf.execute("show interfaces | match eth0")
        assert "eth0" in out


# ============================================================================
# Tab completion — show configuration (operational mode)
# ============================================================================

def complete_oper(text: str, store=None) -> list[str]:
    c = NOSCompleter(mode=CLIMode.OPERATIONAL, edit_path=[], store=store)
    doc = Document(text, len(text))
    return [c.text for c in c.get_completions(doc, CompleteEvent())]


class TestShowConfigurationCompletion:
    def test_show_space_includes_configuration(self):
        kws = complete_oper("show ")
        assert "configuration" in kws

    def test_show_conf_partial_completes(self):
        kws = complete_oper("show conf")
        assert "configuration" in kws

    def test_show_configuration_space_shows_sections(self):
        kws = complete_oper("show configuration ")
        assert "system" in kws
        assert "interfaces" in kws
        assert "vlans" in kws
        assert "routing-options" in kws
        assert "protocols" in kws
        assert "policy-options" in kws

    def test_show_configuration_sys_partial(self):
        kws = complete_oper("show configuration sys")
        assert "system" in kws
        assert "interfaces" not in kws

    def test_show_configuration_protocols_space(self):
        kws = complete_oper("show configuration protocols ")
        assert "isis" in kws
        assert "bgp" in kws

    def test_show_configuration_protocols_bgp_space(self):
        kws = complete_oper("show configuration protocols bgp ")
        assert "group" in kws

    def test_show_configuration_routing_options_space(self):
        kws = complete_oper("show configuration routing-options ")
        assert "static" in kws
        assert "router-id" in kws
        assert "autonomous-system" in kws


# ============================================================================
# Tab completion — show <section> (configure mode)
# ============================================================================

def complete_conf(text: str, edit_path=None, store=None) -> list[str]:
    c = NOSCompleter(
        mode=CLIMode.CONFIGURE,
        edit_path=edit_path or [],
        store=store,
    )
    doc = Document(text, len(text))
    return [c.text for c in c.get_completions(doc, CompleteEvent())]


class TestShowSectionConfigureCompletion:
    def test_show_space_offers_sections(self):
        kws = complete_conf("show ")
        assert "system" in kws
        assert "interfaces" in kws
        assert "vlans" in kws
        assert "routing-options" in kws
        assert "protocols" in kws

    def test_show_space_offers_pipe(self):
        kws = complete_conf("show ")
        assert "|" in kws

    def test_show_sys_partial(self):
        kws = complete_conf("show sys")
        assert "system" in kws
        assert "interfaces" not in kws

    def test_show_interfaces_space(self):
        kws = complete_conf("show interfaces ")
        # Should offer dynamic hint for interface names
        assert any("<interface-name>" in k for k in kws)

    def test_show_protocols_space(self):
        kws = complete_conf("show protocols ")
        assert "isis" in kws
        assert "bgp" in kws

    def test_show_protocols_bgp_space(self):
        kws = complete_conf("show protocols bgp ")
        assert "group" in kws

    def test_show_pipe_offers_compare(self):
        kws = complete_conf("show | ")
        assert "compare" in kws

    def test_show_interfaces_pipe_offers_compare_path(self):
        kws = complete_conf("show interfaces | ")
        assert "compare" in kws

    def test_show_section_with_edit_path(self):
        """At (interfaces)#, 'show ' should offer interface children."""
        kws = complete_conf("show ", edit_path=["interfaces"])
        # Should show interface-level completions (description, family, etc.)
        assert "description" in kws or any("<interface-name>" in k for k in kws)

    def test_show_space_at_root_no_operational_cmds(self):
        """Operational show sub-commands (bgp, route) should not appear here."""
        kws = complete_conf("show ")
        # "bgp" and "route" are not top-level config sections
        assert "route" not in kws

    def test_show_routing_options_space(self):
        kws = complete_conf("show routing-options ")
        assert "static" in kws
        assert "router-id" in kws
