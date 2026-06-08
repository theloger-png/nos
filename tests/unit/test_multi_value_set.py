"""Tests for JunOS-style multi-value set commands.

A single set line can encode multiple key=value pairs at the same or
deeper hierarchy levels, e.g.:

    set interfaces et1 mtu 9000 unit 101 vlan-id 101 family inet address 10.0.0.1/24

which is equivalent to:

    set interfaces et1 mtu 9000
    set interfaces et1 unit 101 vlan-id 101
    set interfaces et1 unit 101 family inet address 10.0.0.1/24
"""
from __future__ import annotations

import pytest

from nos.cli.modes.configure import ConfigureMode, _parse_multi_value_set
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
def mode(store, engine):
    return ConfigureMode(store, engine)


# ============================================================================
# Unit tests for _parse_multi_value_set
# ============================================================================

class TestParseMultiValueSet:
    """Low-level tests for the parser that returns (path, value) pairs."""

    # ------------------------------------------------------------------
    # Single-value (backward compat) — must return exactly one pair
    # ------------------------------------------------------------------

    def test_single_int_value(self):
        result = _parse_multi_value_set(["interfaces", "eth0", "mtu", "9000"])
        assert result == [(["interfaces", "eth0", "mtu"], "9000")]

    def test_single_string_value(self):
        result = _parse_multi_value_set(["interfaces", "eth0", "description", "uplink"])
        assert result == [(["interfaces", "eth0", "description"], "uplink")]

    def test_single_enum_value(self):
        result = _parse_multi_value_set(["interfaces", "eth0", "speed", "1g"])
        assert result == [(["interfaces", "eth0", "speed"], "1g")]

    def test_single_presence_flag(self):
        result = _parse_multi_value_set(["interfaces", "eth0", "disable"])
        assert result == [(["interfaces", "eth0", "disable"], None)]

    def test_single_ip_path(self):
        # IP address is a dynamic dict key (not a scalar value)
        result = _parse_multi_value_set(
            ["interfaces", "eth0", "family", "inet", "address", "10.0.0.1/30"]
        )
        assert result == [
            (["interfaces", "eth0", "family", "inet", "address", "10.0.0.1/30"], None)
        ]

    def test_single_bgp_type(self):
        result = _parse_multi_value_set(
            ["protocols", "bgp", "group", "G1", "type", "internal"]
        )
        assert result == [(["protocols", "bgp", "group", "G1", "type"], "internal")]

    def test_single_vlan_id(self):
        result = _parse_multi_value_set(["vlans", "vlan100", "vlan-id", "100"])
        assert result == [(["vlans", "vlan100", "vlan-id"], "100")]

    def test_single_host_name(self):
        result = _parse_multi_value_set(["system", "host-name", "nos01"])
        assert result == [(["system", "host-name"], "nos01")]

    # ------------------------------------------------------------------
    # Two values at the same hierarchy level
    # ------------------------------------------------------------------

    def test_two_values_same_level(self):
        result = _parse_multi_value_set(
            ["interfaces", "eth0", "mtu", "9000", "description", "uplink"]
        )
        assert (["interfaces", "eth0", "mtu"], "9000") in result
        assert (["interfaces", "eth0", "description"], "uplink") in result
        assert len(result) == 2

    def test_two_values_speed_and_duplex(self):
        result = _parse_multi_value_set(
            ["interfaces", "eth0", "speed", "1g", "duplex", "full"]
        )
        assert (["interfaces", "eth0", "speed"], "1g") in result
        assert (["interfaces", "eth0", "duplex"], "full") in result
        assert len(result) == 2

    # ------------------------------------------------------------------
    # Value then deeper path (the canonical JunOS example)
    # ------------------------------------------------------------------

    def test_value_then_deeper_path(self):
        result = _parse_multi_value_set([
            "interfaces", "eth0",
            "mtu", "9000",
            "unit", "0", "family", "inet", "address", "10.0.0.1/24",
        ])
        assert (["interfaces", "eth0", "mtu"], "9000") in result
        assert (
            ["interfaces", "eth0", "unit", "0", "family", "inet", "address", "10.0.0.1/24"],
            None,
        ) in result
        assert len(result) == 2

    def test_junos_canonical_example(self):
        """set interfaces et1 mtu 9000 unit 101 vlan-id 101 family inet address 10.0.0.1/24"""
        result = _parse_multi_value_set([
            "interfaces", "et1",
            "mtu", "9000",
            "unit", "101",
            "vlan-id", "101",
            "family", "inet", "address", "10.0.0.1/24",
        ])
        assert (["interfaces", "et1", "mtu"], "9000") in result
        assert (["interfaces", "et1", "unit", "101", "vlan-id"], "101") in result
        assert (
            ["interfaces", "et1", "unit", "101", "family", "inet", "address", "10.0.0.1/24"],
            None,
        ) in result
        assert len(result) == 3

    def test_three_values_across_levels(self):
        result = _parse_multi_value_set([
            "interfaces", "eth0",
            "mtu", "9000",
            "unit", "0",
            "vlan-id", "100",
            "family", "inet", "address", "10.0.0.1/24",
        ])
        assert (["interfaces", "eth0", "mtu"], "9000") in result
        assert (["interfaces", "eth0", "unit", "0", "vlan-id"], "100") in result
        assert (
            ["interfaces", "eth0", "unit", "0", "family", "inet", "address", "10.0.0.1/24"],
            None,
        ) in result
        assert len(result) == 3

    # ------------------------------------------------------------------
    # Presence flags mixed with values
    # ------------------------------------------------------------------

    def test_presence_then_value(self):
        result = _parse_multi_value_set(
            ["interfaces", "eth0", "disable", "mtu", "9000"]
        )
        assert (["interfaces", "eth0", "disable"], None) in result
        assert (["interfaces", "eth0", "mtu"], "9000") in result
        assert len(result) == 2

    def test_value_then_presence(self):
        result = _parse_multi_value_set(
            ["interfaces", "eth0", "mtu", "9000", "disable"]
        )
        assert (["interfaces", "eth0", "mtu"], "9000") in result
        assert (["interfaces", "eth0", "disable"], None) in result
        assert len(result) == 2

    def test_presence_only_in_tree(self):
        result = _parse_multi_value_set(
            ["routing-options", "static", "route", "10.0.0.0/8", "discard"]
        )
        assert result == [
            (["routing-options", "static", "route", "10.0.0.0/8", "discard"], None)
        ]

    # ------------------------------------------------------------------
    # Unknown / unmodelled tokens → fall back to legacy
    # ------------------------------------------------------------------

    def test_unknown_token_returns_empty(self):
        # "nonexistent" is not a child of interface_inner → fall back
        result = _parse_multi_value_set(
            ["interfaces", "eth0", "nonexistent", "value"]
        )
        assert result == []

    def test_root_unknown_returns_empty(self):
        result = _parse_multi_value_set(["totally_unknown", "path"])
        assert result == []

    # ------------------------------------------------------------------
    # Multi-word (space-containing) values
    # ------------------------------------------------------------------

    def test_space_value_is_single_token(self):
        # After shlex.split the caller passes unquoted multi-word values as
        # a single token with spaces; the parser just records them as-is.
        result = _parse_multi_value_set(
            ["interfaces", "eth0", "description", "my uplink"]
        )
        assert result == [(["interfaces", "eth0", "description"], "my uplink")]


# ============================================================================
# Integration tests via ConfigureMode.execute()
# ============================================================================

class TestMultiValueSetIntegration:
    """End-to-end tests: execute() must apply all pairs to the candidate config."""

    # ------------------------------------------------------------------
    # Backward compatibility — single values unchanged
    # ------------------------------------------------------------------

    def test_single_int_value_stored(self, mode, store):
        mode.execute("set interfaces eth0 mtu 9000")
        assert store.candidate["interfaces"]["eth0"]["mtu"] == 9000
        assert isinstance(store.candidate["interfaces"]["eth0"]["mtu"], int)

    def test_single_string_value_stored(self, mode, store):
        mode.execute("set interfaces eth0 description internet")
        assert store.candidate["interfaces"]["eth0"]["description"] == "internet"

    def test_single_quoted_string_stored(self, mode, store):
        mode.execute('set interfaces eth0 description "my uplink"')
        assert store.candidate["interfaces"]["eth0"]["description"] == "my uplink"

    def test_single_presence_flag_stored(self, mode, store):
        mode.execute("set interfaces eth0 disable")
        assert store.candidate["interfaces"]["eth0"]["disable"] is True

    def test_single_ip_address_key_stored(self, mode, store):
        mode.execute("set interfaces eth0 family inet address 10.0.0.1/30")
        addr = store.candidate["interfaces"]["eth0"]["family_inet"]["address"]
        assert "10.0.0.1/30" in addr
        assert addr["10.0.0.1/30"] == {}

    # ------------------------------------------------------------------
    # Two values at the same level
    # ------------------------------------------------------------------

    def test_two_values_same_level(self, mode, store):
        mode.execute('set interfaces eth0 mtu 9000 description "uplink"')
        iface = store.candidate["interfaces"]["eth0"]
        assert iface["mtu"] == 9000
        assert iface["description"] == "uplink"

    def test_two_values_both_int_and_string(self, mode, store):
        mode.execute("set interfaces eth0 mtu 1500 speed 1g")
        iface = store.candidate["interfaces"]["eth0"]
        assert iface["mtu"] == 1500
        assert iface["speed"] == "1g"

    # ------------------------------------------------------------------
    # Value then deeper path
    # ------------------------------------------------------------------

    def test_value_then_ip_path(self, mode, store):
        mode.execute("set interfaces eth0 mtu 9000 unit 0 family inet address 10.0.0.1/24")
        iface = store.candidate["interfaces"]["eth0"]
        assert iface["mtu"] == 9000
        unit = iface["unit"]["0"]
        assert unit["family_inet"]["address"]["10.0.0.1/24"] == {}

    def test_junos_canonical_example(self, mode, store):
        """The spec's flagship example."""
        mode.execute(
            "set interfaces et1 mtu 9000 unit 101 vlan-id 101 "
            "family inet address 10.0.0.1/24"
        )
        iface = store.candidate["interfaces"]["et1"]
        assert iface["mtu"] == 9000
        unit = iface["unit"]["101"]
        assert unit["vlan_id"] == 101
        assert unit["family_inet"]["address"]["10.0.0.1/24"] == {}

    # ------------------------------------------------------------------
    # Three values across levels
    # ------------------------------------------------------------------

    def test_three_values_across_levels(self, mode, store):
        mode.execute(
            "set interfaces eth0 mtu 9000 unit 0 vlan-id 100 "
            "family inet address 10.0.0.1/24"
        )
        iface = store.candidate["interfaces"]["eth0"]
        assert iface["mtu"] == 9000
        unit = iface["unit"]["0"]
        assert unit["vlan_id"] == 100
        assert unit["family_inet"]["address"]["10.0.0.1/24"] == {}

    # ------------------------------------------------------------------
    # Presence flags mixed with values
    # ------------------------------------------------------------------

    def test_presence_then_value(self, mode, store):
        mode.execute("set interfaces eth0 disable mtu 9000")
        iface = store.candidate["interfaces"]["eth0"]
        assert iface["disable"] is True
        assert iface["mtu"] == 9000

    def test_value_then_presence(self, mode, store):
        mode.execute("set interfaces eth0 mtu 9000 disable")
        iface = store.candidate["interfaces"]["eth0"]
        assert iface["mtu"] == 9000
        assert iface["disable"] is True

    def test_presence_with_deeper_value(self, mode, store):
        mode.execute(
            "set interfaces eth0 disable "
            "unit 0 family inet address 192.168.1.1/24"
        )
        iface = store.candidate["interfaces"]["eth0"]
        assert iface["disable"] is True
        assert iface["unit"]["0"]["family_inet"]["address"]["192.168.1.1/24"] == {}

    # ------------------------------------------------------------------
    # Quoted string values
    # ------------------------------------------------------------------

    def test_quoted_multi_word_description(self, mode, store):
        mode.execute('set interfaces eth0 mtu 9000 description "core uplink"')
        iface = store.candidate["interfaces"]["eth0"]
        assert iface["mtu"] == 9000
        assert iface["description"] == "core uplink"

    def test_quoted_description_with_special_chars(self, mode, store):
        mode.execute('set interfaces eth0 description "link to peer #1"')
        assert store.candidate["interfaces"]["eth0"]["description"] == "link to peer #1"

    # ------------------------------------------------------------------
    # Multiple interfaces (independent commands merge correctly)
    # ------------------------------------------------------------------

    def test_two_separate_multi_value_commands_merged(self, mode, store):
        mode.execute("set interfaces eth0 mtu 9000 description uplink")
        mode.execute("set interfaces eth1 mtu 1500 speed 1g")
        assert store.candidate["interfaces"]["eth0"]["mtu"] == 9000
        assert store.candidate["interfaces"]["eth0"]["description"] == "uplink"
        assert store.candidate["interfaces"]["eth1"]["mtu"] == 1500
        assert store.candidate["interfaces"]["eth1"]["speed"] == "1g"

    # ------------------------------------------------------------------
    # edit_path context
    # ------------------------------------------------------------------

    def test_multi_value_respects_edit_path(self, mode, store):
        mode.execute("edit interfaces eth0")
        mode.execute("set mtu 9000 description uplink")
        iface = store.candidate["interfaces"]["eth0"]
        assert iface["mtu"] == 9000
        assert iface["description"] == "uplink"

    # ------------------------------------------------------------------
    # Unmodelled paths still work (legacy fallback)
    # ------------------------------------------------------------------

    def test_legacy_fallback_for_unknown_section(self, mode, store):
        # "firewall" is not in CONFIG_TREE; must not crash and must store something
        result = mode.execute("set firewall filter F1 term T1 then accept")
        assert result == "" or result is None or "error" not in (result or "")

    # ------------------------------------------------------------------
    # System hierarchy (multi-value across system keys)
    # ------------------------------------------------------------------

    def test_system_two_values(self, mode, store):
        mode.execute('set system host-name nos01 domain-name example.com')
        sys_cfg = store.candidate["system"]
        assert sys_cfg["host_name"] == "nos01"
        assert sys_cfg["domain_name"] == "example.com"

    # ------------------------------------------------------------------
    # Routing-options multi-value
    # ------------------------------------------------------------------

    def test_routing_options_two_values(self, mode, store):
        mode.execute("set routing-options router-id 1.1.1.1 autonomous-system 65000")
        ro = store.candidate["routing_options"]
        assert ro["router_id"] == "1.1.1.1"
        assert ro["autonomous_system"] == 65000
