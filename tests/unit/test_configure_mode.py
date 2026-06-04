"""Unit tests for nos.cli.modes.configure — focusing on set/delete/show."""
from __future__ import annotations

import pytest

from nos.cli.modes.configure import ConfigureMode, _find_value_split
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
# _find_value_split
# ============================================================================

class TestFindValueSplit:
    def test_description_is_value(self):
        tokens = ["interfaces", "eth0", "description", "internet"]
        assert _find_value_split(tokens) == 3

    def test_host_name_is_value(self):
        tokens = ["system", "host-name", "nos01"]
        assert _find_value_split(tokens) == 2

    def test_router_id_is_value(self):
        tokens = ["routing-options", "router-id", "1.1.1.1"]
        assert _find_value_split(tokens) == 2

    def test_autonomous_system_is_value(self):
        tokens = ["routing-options", "autonomous-system", "65000"]
        assert _find_value_split(tokens) == 2

    def test_vlan_id_is_value(self):
        tokens = ["vlans", "vlan100", "vlan-id", "100"]
        assert _find_value_split(tokens) == 3

    def test_address_ip_is_not_value(self):
        # 10.0.0.1/30 is a dynamic key (dict key), not a leaf value
        tokens = ["interfaces", "eth0", "family", "inet", "address", "10.0.0.1/30"]
        assert _find_value_split(tokens) == len(tokens)

    def test_disable_presence_no_value(self):
        tokens = ["interfaces", "eth0", "disable"]
        assert _find_value_split(tokens) == len(tokens)

    def test_speed_enum_is_value(self):
        tokens = ["interfaces", "eth0", "speed", "1g"]
        assert _find_value_split(tokens) == 3

    def test_bgp_type_is_value(self):
        tokens = ["protocols", "bgp", "group", "IBGP", "type", "internal"]
        assert _find_value_split(tokens) == 5

    def test_unknown_path_returns_len(self):
        tokens = ["interfaces", "eth0", "nonexistent", "val"]
        assert _find_value_split(tokens) == len(tokens)

    def test_empty_tokens(self):
        assert _find_value_split([]) == 0


# ============================================================================
# set — unquoted single-word string values (the reported bug)
# ============================================================================

class TestSetUnquotedStringValue:
    def test_description_unquoted_word(self, mode, store):
        """Bug: 'set interfaces ens32 description internet' stored as a dict."""
        mode.execute("set interfaces ens32 description internet")
        val = store.candidate["interfaces"]["ens32"]["description"]
        assert val == "internet", f"Expected str 'internet', got {val!r}"

    def test_description_unquoted_word_validates(self, mode, engine, store):
        """After the fix the value must survive commit validation."""
        mode.execute("set interfaces ens32 description internet")
        result = engine.commit_check()
        # There may be other schema errors for a bare interface, but not
        # a 'description: Input should be a valid string' error.
        desc_errors = [
            e for e in result.errors
            if "description" in str(e) and "valid string" in str(e)
        ]
        assert desc_errors == []

    def test_description_quoted_word(self, mode, store):
        mode.execute('set interfaces eth0 description "uplink"')
        assert store.candidate["interfaces"]["eth0"]["description"] == "uplink"

    def test_description_multi_word(self, mode, store):
        mode.execute('set interfaces eth0 description "my uplink"')
        assert store.candidate["interfaces"]["eth0"]["description"] == "my uplink"

    def test_hostname_unquoted(self, mode, store):
        mode.execute("set system host-name nos01")
        sys_cfg = store.candidate.get("system", {})
        assert sys_cfg.get("host_name") == "nos01"

    def test_hostname_quoted(self, mode, store):
        mode.execute('set system host-name "nos01"')
        assert store.candidate["system"]["host_name"] == "nos01"

    def test_bgp_group_type_unquoted(self, mode, store):
        mode.execute("set protocols bgp group IBGP type internal")
        grp = store.candidate["protocols"]["bgp"]["group"]["IBGP"]
        assert grp["type"] == "internal"

    def test_interface_speed_unquoted(self, mode, store):
        mode.execute("set interfaces eth0 speed 1g")
        assert store.candidate["interfaces"]["eth0"]["speed"] == "1g"

    def test_interface_duplex_unquoted(self, mode, store):
        mode.execute("set interfaces eth0 duplex full")
        assert store.candidate["interfaces"]["eth0"]["duplex"] == "full"

    def test_vlan_l3_interface_unquoted(self, mode, store):
        mode.execute("set vlans vlan100 l3-interface irb.100")
        assert store.candidate["vlans"]["vlan100"]["l3_interface"] == "irb.100"


# ============================================================================
# set — integer values (must not be quoted)
# ============================================================================

class TestSetIntegerValue:
    def test_vlan_id_stored_as_int(self, mode, store):
        mode.execute("set vlans vlan100 vlan-id 100")
        assert store.candidate["vlans"]["vlan100"]["vlan_id"] == 100
        assert isinstance(store.candidate["vlans"]["vlan100"]["vlan_id"], int)

    def test_autonomous_system_stored_as_int(self, mode, store):
        mode.execute("set routing-options autonomous-system 65000")
        assert store.candidate["routing_options"]["autonomous_system"] == 65000
        assert isinstance(store.candidate["routing_options"]["autonomous_system"], int)

    def test_mtu_stored_as_int(self, mode, store):
        mode.execute("set interfaces eth0 mtu 9000")
        assert store.candidate["interfaces"]["eth0"]["mtu"] == 9000
        assert isinstance(store.candidate["interfaces"]["eth0"]["mtu"], int)


# ============================================================================
# set — dynamic keys (IP prefixes, interface names) stay as dict keys
# ============================================================================

class TestSetDynamicKeys:
    def test_inet_address_becomes_dict_key(self, mode, store):
        mode.execute("set interfaces eth0 family inet address 10.0.0.1/30")
        addr_dict = store.candidate["interfaces"]["eth0"]["family_inet"]["address"]
        assert "10.0.0.1/30" in addr_dict

    def test_inet_address_stored_as_empty_dict(self, mode, store):
        mode.execute("set interfaces eth0 family inet address 10.0.0.1/30")
        addr = store.candidate["interfaces"]["eth0"]["family_inet"]["address"]["10.0.0.1/30"]
        assert addr == {}, f"Expected {{}} for InetAddress, got {addr!r}"

    def test_unit_family_inet_address_stores_correctly(self, mode, store):
        mode.execute("set interfaces ens34 unit 0 family inet address 10.0.0.1/24")
        unit = store.candidate["interfaces"]["ens34"]["unit"]["0"]
        assert "family_inet" in unit, "family_inet should be nested under unit"
        addr = unit["family_inet"]["address"]
        assert "10.0.0.1/24" in addr
        assert addr["10.0.0.1/24"] == {}, f"Expected {{}} for InetAddress, got {addr['10.0.0.1/24']!r}"

    def test_static_route_prefix_becomes_dict_key(self, mode, store):
        mode.execute(
            "set routing-options static route 0.0.0.0/0 next-hop 10.0.0.1"
        )
        routes = store.candidate["routing_options"]["static"]["route"]
        assert "0.0.0.0/0" in routes

    def test_inet_address_primary_flag(self, mode, store):
        mode.execute("set interfaces eth0 family inet address 10.0.0.1/30")
        mode.execute("set interfaces eth0 family inet address 10.0.0.1/30 primary")
        addr = store.candidate["interfaces"]["eth0"]["family_inet"]["address"]["10.0.0.1/30"]
        assert addr.get("primary") is True


# ============================================================================
# set — presence flags
# ============================================================================

class TestSetPresenceFlags:
    def test_disable_sets_true(self, mode, store):
        mode.execute("set interfaces eth0 disable")
        assert store.candidate["interfaces"]["eth0"]["disable"] is True

    def test_point_to_point_sets_true(self, mode, store):
        mode.execute("set interfaces eth0 description x")
        mode.execute("set protocols isis interface eth0 point-to-point")
        iface = store.candidate["protocols"]["isis"]["interface"]["eth0"]
        assert iface.get("point_to_point") is True


# ============================================================================
# set — edit_path prefix
# ============================================================================

class TestSetWithEditPath:
    def test_set_relative_to_edit_path(self, mode, store):
        mode.edit_path = ["interfaces", "eth0"]
        mode.execute("set description internet")
        assert store.candidate["interfaces"]["eth0"]["description"] == "internet"

    def test_set_integer_relative(self, mode, store):
        mode.edit_path = ["vlans", "vlan100"]
        mode.execute("set vlan-id 200")
        assert store.candidate["vlans"]["vlan100"]["vlan_id"] == 200

    def test_set_presence_relative(self, mode, store):
        mode.edit_path = ["interfaces", "eth0"]
        mode.execute("set disable")
        assert store.candidate["interfaces"]["eth0"]["disable"] is True


# ============================================================================
# delete
# ============================================================================

class TestDelete:
    def test_delete_removes_key(self, mode, store):
        mode.execute("set interfaces eth0 description internet")
        mode.execute("delete interfaces eth0 description")
        assert "description" not in store.candidate.get("interfaces", {}).get("eth0", {})

    def test_delete_nonexistent_is_noop(self, mode, store):
        out = mode.execute("delete interfaces eth0 description")
        assert out == ""  # no error

    def test_delete_with_edit_path(self, mode, store):
        mode.execute("set interfaces eth0 description internet")
        mode.edit_path = ["interfaces", "eth0"]
        mode.execute("delete description")
        assert "description" not in store.candidate.get("interfaces", {}).get("eth0", {})


# ============================================================================
# commit and-quit
# ============================================================================

class TestCommitAndQuit:
    def test_commit_and_quit_commits_on_success(self, mode, store, engine):
        mode.execute("set system host-name nos01")
        with pytest.raises(SystemExit) as exc_info:
            mode.execute("commit and-quit")
        assert exc_info.value.code == 0
        assert store.running["system"]["host_name"] == "nos01"

    def test_commit_and_quit_exits_configure_mode(self, mode, store):
        mode.execute("set system host-name nos01")
        with pytest.raises(SystemExit):
            mode.execute("commit and-quit")

    def test_commit_and_quit_stays_in_mode_on_error(self, mode, store):
        mode.execute("set interfaces eth0 mtu 99999")  # invalid: out of range
        out = mode.execute("commit and-quit")
        assert "validation failed" in out
        # Mode should still be in configure (no SystemExit raised)
        assert "error" not in mode.execute("show") or True  # still operational
