"""Unit tests for nos.config.serializer."""
import pytest

from nos.config.serializer import to_set_commands, from_set_commands


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def has_cmd(cmds: list[str], fragment: str) -> bool:
    return any(fragment in c for c in cmds)


# ---------------------------------------------------------------------------
# to_set_commands — basic types
# ---------------------------------------------------------------------------

def test_empty_config_produces_no_commands():
    assert to_set_commands({}) == []


def test_string_value_is_quoted():
    cmds = to_set_commands({"system": {"host_name": "router01"}})
    assert has_cmd(cmds, 'set system host-name "router01"')


def test_integer_value_is_unquoted():
    cmds = to_set_commands({"interfaces": {"eth0": {"mtu": 1500}}})
    assert has_cmd(cmds, "set interfaces eth0 mtu 1500")


def test_boolean_true_emitted_as_bare_path():
    cmds = to_set_commands({"interfaces": {"eth0": {"disable": True}}})
    assert has_cmd(cmds, "set interfaces eth0 disable")
    # No trailing value after "disable"
    line = next(c for c in cmds if "disable" in c)
    assert line.endswith("disable")


def test_boolean_false_not_emitted():
    cmds = to_set_commands({"interfaces": {"eth0": {"disable": False}}})
    assert not any("disable" in c for c in cmds)


def test_none_value_not_emitted():
    cmds = to_set_commands({"system": {"host_name": None}})
    assert cmds == []


# ---------------------------------------------------------------------------
# to_set_commands — key conversion
# ---------------------------------------------------------------------------

def test_snake_case_keys_become_hyphenated():
    cmds = to_set_commands({"routing_options": {"router_id": "1.1.1.1"}})
    assert has_cmd(cmds, "routing-options router-id")
    assert not any("routing_options" in c or "router_id" in c for c in cmds)


def test_deeply_nested_keys_converted():
    cfg = {
        "protocols": {
            "bgp": {
                "group": {
                    "UPSTREAM": {
                        "group_type": "external",
                        "peer_as": 65001,
                    }
                }
            }
        }
    }
    cmds = to_set_commands(cfg)
    assert has_cmd(cmds, "set protocols bgp group UPSTREAM group-type")
    assert has_cmd(cmds, "set protocols bgp group UPSTREAM peer-as 65001")


# ---------------------------------------------------------------------------
# to_set_commands — lists
# ---------------------------------------------------------------------------

def test_list_items_emit_separate_commands():
    cmds = to_set_commands({"system": {"name_server": ["8.8.8.8", "8.8.4.4"]}})
    assert has_cmd(cmds, '"8.8.8.8"')
    assert has_cmd(cmds, '"8.8.4.4"')
    assert sum(1 for c in cmds if "name-server" in c) == 2


def test_empty_list_produces_no_commands():
    cmds = to_set_commands({"system": {"name_server": []}})
    assert not any("name-server" in c for c in cmds)


# ---------------------------------------------------------------------------
# to_set_commands — presence / empty dict
# ---------------------------------------------------------------------------

def test_empty_dict_value_emits_bare_path():
    cmds = to_set_commands({"protocols": {"bgp": {"group": {"PEER": {}}}}})
    assert has_cmd(cmds, "set protocols bgp group PEER")


def test_nested_address_dict_key_is_path_component():
    # {} is what the store holds for a default InetAddress (primary=False is omitted)
    cfg = {
        "interfaces": {
            "eth0": {
                "unit": {
                    "0": {
                        "family_inet": {
                            "address": {
                                "10.0.0.1/30": {}
                            }
                        }
                    }
                }
            }
        }
    }
    cmds = to_set_commands(cfg)
    # empty dict at leaf → address key becomes the presence path component
    assert has_cmd(cmds, "set interfaces eth0 unit 0 family inet address 10.0.0.1/30")


def test_address_with_primary_true():
    cfg = {
        "interfaces": {
            "eth0": {
                "unit": {"0": {"family_inet": {"address": {"10.0.0.1/30": {"primary": True}}}}}
            }
        }
    }
    cmds = to_set_commands(cfg)
    assert has_cmd(cmds, "10.0.0.1/30 primary")


# ---------------------------------------------------------------------------
# to_set_commands — output is sorted
# ---------------------------------------------------------------------------

def test_output_is_sorted():
    cfg = {
        "system": {"host_name": "r1"},
        "interfaces": {"eth0": {"mtu": 1500}},
    }
    cmds = to_set_commands(cfg)
    assert cmds == sorted(cmds)


# ---------------------------------------------------------------------------
# from_set_commands — basic types
# ---------------------------------------------------------------------------

def test_parse_quoted_string():
    result = from_set_commands(['set system host-name "router01"'])
    assert result["system"]["host_name"] == "router01"


def test_parse_integer():
    result = from_set_commands(["set interfaces eth0 mtu 1500"])
    assert result["interfaces"]["eth0"]["mtu"] == 1500


def test_parse_presence_flag():
    result = from_set_commands(["set interfaces eth0 disable"])
    assert result["interfaces"]["eth0"]["disable"] is True


def test_parse_ignores_non_set_lines():
    result = from_set_commands(["# comment", "delete system", "set system host-name \"r1\""])
    assert "system" in result
    assert "host_name" in result["system"]


def test_parse_empty_list():
    assert from_set_commands([]) == {}


# ---------------------------------------------------------------------------
# from_set_commands — key conversion
# ---------------------------------------------------------------------------

def test_hyphenated_keys_become_snake_case():
    result = from_set_commands(['set routing-options router-id "1.1.1.1"'])
    assert "routing_options" in result
    assert "router_id" in result["routing_options"]
    assert result["routing_options"]["router_id"] == "1.1.1.1"


# ---------------------------------------------------------------------------
# from_set_commands — nested structures
# ---------------------------------------------------------------------------

def test_parse_nested_dict():
    cmds = [
        'set protocols bgp group UPSTREAM group-type "external"',
        "set protocols bgp group UPSTREAM peer-as 65001",
    ]
    result = from_set_commands(cmds)
    grp = result["protocols"]["bgp"]["group"]["UPSTREAM"]
    assert grp["group_type"] == "external"
    assert grp["peer_as"] == 65001


def test_parse_ip_as_dict_key_presence():
    result = from_set_commands(["set interfaces eth0 unit 0 family inet address 10.0.0.1/30"])
    addr = result["interfaces"]["eth0"]["unit"]["0"]["family_inet"]["address"]
    assert "10.0.0.1/30" in addr
    assert addr["10.0.0.1/30"] is True


# ---------------------------------------------------------------------------
# from_set_commands — repeated keys (list accumulation)
# ---------------------------------------------------------------------------

def test_repeated_scalar_becomes_list():
    cmds = [
        'set system name-server "8.8.8.8"',
        'set system name-server "8.8.4.4"',
    ]
    result = from_set_commands(cmds)
    ns = result["system"]["name_server"]
    assert isinstance(ns, list)
    assert "8.8.8.8" in ns
    assert "8.8.4.4" in ns


# ---------------------------------------------------------------------------
# Round-trip: to_set_commands → from_set_commands → to_set_commands
# ---------------------------------------------------------------------------

def test_roundtrip_simple_system():
    cfg = {"system": {"host_name": "r1"}}
    assert to_set_commands(from_set_commands(to_set_commands(cfg))) == to_set_commands(cfg)


def test_roundtrip_interface_with_mtu_and_flag():
    cfg = {"interfaces": {"eth0": {"mtu": 9000, "disable": True}}}
    assert to_set_commands(from_set_commands(to_set_commands(cfg))) == to_set_commands(cfg)


def test_roundtrip_bgp_group():
    cfg = {
        "protocols": {
            "bgp": {
                "group": {
                    "TRANSIT": {
                        "group_type": "external",
                        "peer_as": 65100,
                        "local_as": 65000,
                    }
                }
            }
        }
    }
    original_cmds = to_set_commands(cfg)
    recovered = from_set_commands(original_cmds)
    assert to_set_commands(recovered) == original_cmds


def test_roundtrip_routing_options():
    cfg = {
        "routing_options": {
            "router_id": "10.0.0.1",
            "autonomous_system": 65001,
        }
    }
    original_cmds = to_set_commands(cfg)
    recovered = from_set_commands(original_cmds)
    assert to_set_commands(recovered) == original_cmds


def test_roundtrip_static_route():
    cfg = {
        "routing_options": {
            "static": {
                "route": {
                    "0.0.0.0/0": {"next_hop": "192.168.1.1"}
                }
            }
        }
    }
    original_cmds = to_set_commands(cfg)
    recovered = from_set_commands(original_cmds)
    assert to_set_commands(recovered) == original_cmds


def test_roundtrip_vlan():
    cfg = {
        "vlans": {
            "mgmt": {"vlan_id": 10, "description": "Management VLAN"}
        }
    }
    original_cmds = to_set_commands(cfg)
    recovered = from_set_commands(original_cmds)
    assert to_set_commands(recovered) == original_cmds


# ---------------------------------------------------------------------------
# Quoted string edge cases
# ---------------------------------------------------------------------------

def test_string_with_spaces_quoted_and_parsed():
    cfg = {"system": {"host_name": "my router"}}
    cmds = to_set_commands(cfg)
    assert '"my router"' in cmds[0]
    result = from_set_commands(cmds)
    assert result["system"]["host_name"] == "my router"


def test_empty_string_roundtrip():
    cfg = {"system": {"host_name": ""}}
    cmds = to_set_commands(cfg)
    result = from_set_commands(cmds)
    assert result["system"]["host_name"] == ""
