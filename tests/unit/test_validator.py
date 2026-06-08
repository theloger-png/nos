"""Unit tests for ConfigValidator — Phase 1 validation."""
import pytest

from nos.config.validator import ConfigValidator


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def validate(config: dict):
    return ConfigValidator().validate(config)


def assert_valid(config: dict):
    result = validate(config)
    assert result.is_valid, f"Expected valid but got errors: {result.errors}"


def assert_invalid(config: dict, *, path_contains: str = "", msg_contains: str = ""):
    result = validate(config)
    assert not result.is_valid, "Expected invalid but got valid"
    if path_contains or msg_contains:
        matched = any(
            (path_contains in e.path or not path_contains)
            and (msg_contains.lower() in e.message.lower() or not msg_contains)
            for e in result.errors
        )
        assert matched, (
            f"No error matched path_contains={path_contains!r} "
            f"msg_contains={msg_contains!r}. Errors: {result.errors}"
        )


# ---------------------------------------------------------------------------
# Baseline
# ---------------------------------------------------------------------------

def test_empty_config_is_valid():
    assert_valid({})


def test_minimal_valid_system():
    assert_valid({"system": {"host_name": "router01"}})


def test_full_realistic_config():
    config = {
        "system": {"host_name": "sw01", "domain_name": "lab.local"},
        "interfaces": {
            "eth0": {
                "description": "uplink",
                "mtu": 1500,
                "family_inet": {"address": {"10.0.0.1/30": {}}},
            },
            "eth1": {
                "unit": {
                    "0": {
                        "family_ethernet_switching": {
                            "interface_mode": "access",
                            "vlan": {"members": ["vlan100"]},
                        }
                    }
                }
            },
            "irb": {
                "unit": {
                    "100": {
                        "family_inet": {"address": {"192.168.100.1/24": {}}}
                    }
                }
            },
        },
        "vlans": {
            "vlan100": {"vlan_id": 100, "l3_interface": "irb.100"},
        },
        "routing_options": {
            "router_id": "10.0.0.1",
            "autonomous_system": 65000,
            "static": {"route": {"0.0.0.0/0": {"next_hop": "10.0.0.2"}}},
        },
        "protocols": {
            "isis": {"interface": {"eth0": {"point_to_point": True}}},
            "bgp": {
                "group": {
                    "IBGP": {
                        "group_type": "internal",
                        "local_address": "10.0.0.1",
                        "neighbor": {"2.2.2.2": {}},
                        "export": "EXPORT-STATIC",
                    }
                }
            },
        },
        "policy_options": {
            "prefix_list": {"DEFAULT": ["0.0.0.0/0"]},
            "policy_statement": {
                "EXPORT-STATIC": {
                    "term": {
                        "1": {
                            "from_config": {"protocol": "static"},
                            "then_config": {"accept": True},
                        }
                    }
                }
            },
        },
    }
    assert_valid(config)


# ---------------------------------------------------------------------------
# VLAN ID range
# ---------------------------------------------------------------------------

def test_vlan_id_zero_fails():
    assert_invalid(
        {"vlans": {"v0": {"vlan_id": 0}}},
        path_contains="vlan_id",
        msg_contains="greater than or equal to 1",
    )


def test_vlan_id_4095_fails():
    assert_invalid(
        {"vlans": {"vhigh": {"vlan_id": 4095}}},
        path_contains="vlan_id",
        msg_contains="less than or equal to 4094",
    )


def test_vlan_id_boundary_valid():
    assert_valid({"vlans": {"v1": {"vlan_id": 1}}})
    assert_valid({"vlans": {"v4094": {"vlan_id": 4094}}})


def test_vlan_missing_vlan_id_fails():
    assert_invalid(
        {"vlans": {"myvlan": {"description": "no id here"}}},
        path_contains="vlan_id",
        msg_contains="required",
    )


# ---------------------------------------------------------------------------
# MTU range
# ---------------------------------------------------------------------------

def test_mtu_255_fails():
    assert_invalid(
        {"interfaces": {"eth0": {"mtu": 255}}},
        path_contains="mtu",
    )


def test_mtu_9193_fails():
    assert_invalid(
        {"interfaces": {"eth0": {"mtu": 9193}}},
        path_contains="mtu",
    )


def test_mtu_boundary_valid():
    assert_valid({"interfaces": {"eth0": {"mtu": 256}}})
    assert_valid({"interfaces": {"eth0": {"mtu": 9192}}})


# ---------------------------------------------------------------------------
# Switchport XOR routed port mutual exclusivity
# ---------------------------------------------------------------------------

def test_switchport_and_routed_port_fails():
    assert_invalid(
        {
            "interfaces": {
                "eth0": {
                    "family_inet": {"address": {"10.0.0.1/30": {}}},
                    "unit": {
                        "0": {
                            "family_ethernet_switching": {
                                "interface_mode": "trunk",
                                "vlan": {"members": ["all"]},
                            }
                        }
                    },
                }
            }
        },
        msg_contains="mutually exclusive",
    )


def test_routed_port_only_valid():
    assert_valid(
        {
            "interfaces": {
                "eth0": {
                    "family_inet": {"address": {"10.0.0.1/30": {}}}
                }
            }
        }
    )


def test_switchport_only_valid():
    assert_valid(
        {
            "interfaces": {
                "eth0": {
                    "unit": {
                        "0": {
                            "family_ethernet_switching": {
                                "interface_mode": "access",
                                "vlan": {"members": ["all"]},
                            }
                        }
                    }
                }
            }
        }
    )


def test_unit_with_family_inet_no_conflict():
    """IRB-style: unit with family_inet (no ethernet-switching) — should be valid."""
    assert_valid(
        {
            "interfaces": {
                "irb": {
                    "unit": {
                        "100": {
                            "family_inet": {"address": {"192.168.100.1/24": {}}}
                        }
                    }
                }
            }
        }
    )


# ---------------------------------------------------------------------------
# IP address validation
# ---------------------------------------------------------------------------

def test_invalid_ip_in_family_inet_address():
    assert_invalid(
        {"interfaces": {"eth0": {"family_inet": {"address": {"not-an-ip": {}}}}}},
        msg_contains="invalid ip",
    )


def test_invalid_router_id():
    assert_invalid(
        {"routing_options": {"router_id": "999.999.999.999"}},
        msg_contains="invalid ip",
    )


def test_invalid_static_next_hop():
    assert_invalid(
        {
            "routing_options": {
                "static": {"route": {"10.0.0.0/8": {"next_hop": "not-an-ip"}}}
            }
        },
        msg_contains="invalid ip",
    )


def test_invalid_static_route_prefix():
    assert_invalid(
        {
            "routing_options": {
                "static": {"route": {"not-a-prefix": {"next_hop": "10.0.0.1"}}}
            }
        },
        msg_contains="invalid ip prefix",
    )


def test_invalid_bgp_neighbor_ip():
    assert_invalid(
        {
            "protocols": {
                "bgp": {
                    "group": {
                        "G1": {
                            "group_type": "internal",
                            "neighbor": {"not-an-ip": {}},
                        }
                    }
                }
            }
        },
        msg_contains="invalid ip",
    )


def test_invalid_bgp_local_address():
    assert_invalid(
        {
            "protocols": {
                "bgp": {
                    "group": {
                        "G1": {
                            "group_type": "internal",
                            "local_address": "bad",
                        }
                    }
                }
            }
        },
        msg_contains="invalid ip",
    )


# ---------------------------------------------------------------------------
# AS number range
# ---------------------------------------------------------------------------

def test_asn_zero_fails():
    assert_invalid(
        {"routing_options": {"autonomous_system": 0}},
        path_contains="autonomous_system",
    )


def test_asn_too_large_fails():
    assert_invalid(
        {"routing_options": {"autonomous_system": 4294967296}},
        path_contains="autonomous_system",
    )


def test_asn_boundary_valid():
    assert_valid({"routing_options": {"autonomous_system": 1}})
    assert_valid({"routing_options": {"autonomous_system": 4294967295}})


# ---------------------------------------------------------------------------
# BGP eBGP peer_as constraint
# ---------------------------------------------------------------------------

def test_ebgp_without_peer_as_fails():
    assert_invalid(
        {
            "protocols": {
                "bgp": {
                    "group": {
                        "EBGP": {
                            "group_type": "external",
                            "neighbor": {"1.2.3.4": {}},
                        }
                    }
                }
            }
        },
        msg_contains="peer_as",
    )


def test_ebgp_with_peer_as_valid():
    assert_valid(
        {
            "protocols": {
                "bgp": {
                    "group": {
                        "EBGP": {
                            "group_type": "external",
                            "peer_as": 65001,
                            "neighbor": {"1.2.3.4": {}},
                        }
                    }
                }
            }
        }
    )


def test_ibgp_without_peer_as_valid():
    assert_valid(
        {
            "protocols": {
                "bgp": {
                    "group": {
                        "IBGP": {
                            "group_type": "internal",
                            "neighbor": {"1.2.3.4": {}},
                        }
                    }
                }
            }
        }
    )


# ---------------------------------------------------------------------------
# l3_interface format validation
# ---------------------------------------------------------------------------

def test_l3_interface_bad_format_fails():
    assert_invalid(
        {"vlans": {"v100": {"vlan_id": 100, "l3_interface": "eth0.100"}}},
        msg_contains="irb",
    )


def test_l3_interface_valid_format():
    # Format check passes; reference check will fail without the irb interface
    result = validate({"vlans": {"v100": {"vlan_id": 100, "l3_interface": "irb.100"}}})
    # The only error should be the missing irb.100 interface reference, not a format error
    assert all("irb" not in e.message.lower() or "does not match" in e.message.lower()
               for e in result.errors)


# ---------------------------------------------------------------------------
# Cross-reference: VLAN member names
# ---------------------------------------------------------------------------

def test_switchport_member_referencing_undefined_vlan_fails():
    assert_invalid(
        {
            "interfaces": {
                "eth0": {
                    "unit": {
                        "0": {
                            "family_ethernet_switching": {
                                "interface_mode": "access",
                                "vlan": {"members": ["nonexistent_vlan"]},
                            }
                        }
                    }
                }
            }
        },
        msg_contains="not defined in vlans",
    )


def test_switchport_member_all_no_reference_needed():
    assert_valid(
        {
            "interfaces": {
                "eth0": {
                    "unit": {
                        "0": {
                            "family_ethernet_switching": {
                                "interface_mode": "trunk",
                                "vlan": {"members": ["all"]},
                            }
                        }
                    }
                }
            }
        }
    )


def test_switchport_member_existing_vlan_valid():
    assert_valid(
        {
            "interfaces": {
                "eth0": {
                    "unit": {
                        "0": {
                            "family_ethernet_switching": {
                                "interface_mode": "access",
                                "vlan": {"members": ["vlan100"]},
                            }
                        }
                    }
                }
            },
            "vlans": {"vlan100": {"vlan_id": 100}},
        }
    )


# ---------------------------------------------------------------------------
# Cross-reference: l3_interface → irb unit
# ---------------------------------------------------------------------------

def test_l3_interface_missing_irb_interface_fails():
    assert_invalid(
        {"vlans": {"v100": {"vlan_id": 100, "l3_interface": "irb.100"}}},
        path_contains="l3_interface",
        msg_contains="does not match",
    )


def test_l3_interface_with_matching_irb_unit_valid():
    assert_valid(
        {
            "interfaces": {
                "irb": {
                    "unit": {
                        "100": {
                            "family_inet": {"address": {"192.168.1.1/24": {}}}
                        }
                    }
                }
            },
            "vlans": {"v100": {"vlan_id": 100, "l3_interface": "irb.100"}},
        }
    )


# ---------------------------------------------------------------------------
# Cross-reference: protocol interface references
# ---------------------------------------------------------------------------

def test_isis_interface_not_in_interfaces_fails():
    assert_invalid(
        {
            "protocols": {
                "isis": {"interface": {"ghost0": {"point_to_point": True}}}
            }
        },
        path_contains="protocols.isis.interface.ghost0",
        msg_contains="not defined in interfaces",
    )


def test_isis_interface_exists_valid():
    assert_valid(
        {
            "interfaces": {"eth0": {"family_inet": {"address": {"10.0.0.1/30": {}}}}},
            "protocols": {"isis": {"interface": {"eth0": {"point_to_point": True}}}},
        }
    )


# ---------------------------------------------------------------------------
# Cross-reference: BGP policy references
# ---------------------------------------------------------------------------

def test_bgp_export_policy_undefined_fails():
    assert_invalid(
        {
            "protocols": {
                "bgp": {
                    "group": {
                        "G1": {
                            "group_type": "internal",
                            "neighbor": {"1.2.3.4": {}},
                            "export": "NONEXISTENT",
                        }
                    }
                }
            }
        },
        path_contains="export",
        msg_contains="not defined in policy_options",
    )


def test_bgp_export_policy_defined_valid():
    assert_valid(
        {
            "protocols": {
                "bgp": {
                    "group": {
                        "G1": {
                            "group_type": "internal",
                            "neighbor": {"1.2.3.4": {}},
                            "export": "MY-POLICY",
                        }
                    }
                }
            },
            "policy_options": {
                "policy_statement": {"MY-POLICY": {"term": {}}}
            },
        }
    )


# ---------------------------------------------------------------------------
# Cross-reference: routing instance interface references
# ---------------------------------------------------------------------------

def test_routing_instance_undefined_interface_fails():
    assert_invalid(
        {
            "routing_instances": {
                "MGMT": {
                    "instance_type": "vrf",
                    "interface": ["ghost0"],
                }
            }
        },
        path_contains="routing_instances.MGMT.interface",
        msg_contains="not defined in interfaces",
    )


def test_routing_instance_defined_interface_valid():
    assert_valid(
        {
            "interfaces": {"eth0": {}},
            "routing_instances": {
                "MGMT": {
                    "instance_type": "vrf",
                    "interface": ["eth0"],
                }
            },
        }
    )


def test_routing_instance_missing_instance_type_fails():
    assert_invalid(
        {
            "routing_instances": {
                "MGMT": {"interface": []}
            }
        },
        path_contains="routing_instances.MGMT.instance_type",
        msg_contains="required",
    )


# ---------------------------------------------------------------------------
# Static route constraints
# ---------------------------------------------------------------------------

def test_static_route_next_hop_and_discard_fails():
    assert_invalid(
        {
            "routing_options": {
                "static": {
                    "route": {
                        "10.0.0.0/8": {"next_hop": "10.0.0.1", "discard": True}
                    }
                }
            }
        },
        msg_contains="only one",
    )


def test_static_route_single_action_valid():
    assert_valid(
        {
            "routing_options": {
                "static": {
                    "route": {"10.0.0.0/8": {"next_hop": "10.0.0.1"}}
                }
            }
        }
    )


# ---------------------------------------------------------------------------
# Cross-reference: policy prefix-list references
# ---------------------------------------------------------------------------

def test_policy_term_undefined_prefix_list_fails():
    assert_invalid(
        {
            "policy_options": {
                "policy_statement": {
                    "PS": {
                        "term": {
                            "t1": {
                                "from_config": {"prefix_list": "MISSING-LIST"}
                            }
                        }
                    }
                }
            }
        },
        msg_contains="not defined in policy_options.prefix_list",
    )


def test_policy_term_defined_prefix_list_valid():
    assert_valid(
        {
            "policy_options": {
                "prefix_list": {"MY-PREFIXES": ["10.0.0.0/8"]},
                "policy_statement": {
                    "PS": {
                        "term": {
                            "t1": {
                                "from_config": {"prefix_list": "MY-PREFIXES"}
                            }
                        }
                    }
                },
            }
        }
    )


# ---------------------------------------------------------------------------
# VLAN members — numeric string and integer IDs
# ---------------------------------------------------------------------------

def test_vlan_member_numeric_string_converts_and_is_valid():
    """A numeric string like '101' should be accepted and treated as VLAN ID 101."""
    assert_valid(
        {
            "interfaces": {
                "eth0": {
                    "unit": {
                        "0": {
                            "family_ethernet_switching": {
                                "interface_mode": "access",
                                "vlan": {"members": ["101"]},
                            }
                        }
                    }
                }
            }
        }
    )


def test_vlan_member_integer_id_is_valid():
    """An integer VLAN ID in members should be accepted directly."""
    assert_valid(
        {
            "interfaces": {
                "eth0": {
                    "unit": {
                        "0": {
                            "family_ethernet_switching": {
                                "interface_mode": "trunk",
                                "vlan": {"members": [100, 200]},
                            }
                        }
                    }
                }
            }
        }
    )


def test_vlan_member_integer_out_of_range_fails():
    """An integer VLAN ID outside 1–4094 should be rejected."""
    assert_invalid(
        {
            "interfaces": {
                "eth0": {
                    "unit": {
                        "0": {
                            "family_ethernet_switching": {
                                "interface_mode": "access",
                                "vlan": {"members": [5000]},
                            }
                        }
                    }
                }
            }
        },
        msg_contains="out of range",
    )


def test_vlan_member_numeric_string_out_of_range_fails():
    """A numeric string like '4095' should be rejected as out of range."""
    assert_invalid(
        {
            "interfaces": {
                "eth0": {
                    "unit": {
                        "0": {
                            "family_ethernet_switching": {
                                "interface_mode": "access",
                                "vlan": {"members": ["4095"]},
                            }
                        }
                    }
                }
            }
        },
        msg_contains="out of range",
    )


# ---------------------------------------------------------------------------
# lo0 loopback interface constraints
# ---------------------------------------------------------------------------

def test_lo0_with_family_inet_is_valid():
    """lo0 with family_inet address is a valid routed loopback."""
    assert_valid(
        {
            "interfaces": {
                "lo0": {
                    "family_inet": {"address": {"1.1.1.1/32": {}}}
                }
            }
        }
    )


def test_lo0_with_family_inet6_is_valid():
    """lo0 with family_inet6 address is valid."""
    assert_valid(
        {
            "interfaces": {
                "lo0": {
                    "family_inet6": {"address": {"::1/128": {}}}
                }
            }
        }
    )


def test_lo0_unit0_with_family_inet_is_valid():
    """lo0 unit 0 with family_inet is the standard JunOS loopback config."""
    assert_valid(
        {
            "interfaces": {
                "lo0": {
                    "unit": {
                        "0": {
                            "family_inet": {"address": {"1.1.1.1/32": {}}}
                        }
                    }
                }
            }
        }
    )


def test_lo0_rejects_ethernet_switching():
    """lo0 must not allow family ethernet-switching on any unit."""
    assert_invalid(
        {
            "interfaces": {
                "lo0": {
                    "unit": {
                        "0": {
                            "family_ethernet_switching": {
                                "interface_mode": "access",
                                "vlan": {"members": ["all"]},
                            }
                        }
                    }
                }
            }
        },
        path_contains="interfaces.lo0.unit.0.family_ethernet_switching",
        msg_contains="does not support family ethernet-switching",
    )


def test_lo1_rejects_ethernet_switching():
    """lo1 (any loN) must not allow family ethernet-switching."""
    assert_invalid(
        {
            "interfaces": {
                "lo1": {
                    "unit": {
                        "0": {
                            "family_ethernet_switching": {
                                "interface_mode": "trunk",
                                "vlan": {"members": ["all"]},
                            }
                        }
                    }
                }
            }
        },
        path_contains="interfaces.lo1.unit.0.family_ethernet_switching",
        msg_contains="does not support family ethernet-switching",
    )


def test_non_loopback_with_ethernet_switching_not_affected():
    """eth0 with ethernet-switching is unaffected by the loopback constraint."""
    assert_valid(
        {
            "interfaces": {
                "eth0": {
                    "unit": {
                        "0": {
                            "family_ethernet_switching": {
                                "interface_mode": "access",
                                "vlan": {"members": ["all"]},
                            }
                        }
                    }
                }
            }
        }
    )


# ---------------------------------------------------------------------------
# family iso — schema validation
# ---------------------------------------------------------------------------

class TestFamilyIsoSchema:
    def test_valid_net_address(self):
        from nos.config.schema import FamilyIso
        f = FamilyIso(address="49.0001.0000.0101.0101.00")
        assert f.address == "49.0001.0000.0101.0101.00"

    def test_no_address_is_valid(self):
        from nos.config.schema import FamilyIso
        f = FamilyIso()
        assert f.address is None

    def test_invalid_net_format_rejected(self):
        from nos.config.schema import FamilyIso
        import pytest
        with pytest.raises(Exception):
            FamilyIso(address="1.2.3.4")

    def test_invalid_net_too_short_rejected(self):
        from nos.config.schema import FamilyIso
        import pytest
        with pytest.raises(Exception):
            FamilyIso(address="49.0001.0000.0101.00")

    def test_interface_config_accepts_family_iso(self):
        from nos.config.schema import InterfaceConfig, FamilyIso
        iface = InterfaceConfig(family_iso=FamilyIso(address="49.0001.0000.0101.0101.00"))
        assert iface.family_iso is not None
        assert iface.family_iso.address == "49.0001.0000.0101.0101.00"
