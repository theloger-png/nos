"""Unit tests for nos.drivers.frr.renderer.FRRRenderer."""
import pytest

from nos.drivers.frr.renderer import FRRRenderer


@pytest.fixture()
def renderer():
    return FRRRenderer()


# ---------------------------------------------------------------------------
# Header
# ---------------------------------------------------------------------------

def test_render_includes_frr_version(renderer):
    out = renderer.render({})
    assert "frr version" in out


def test_render_includes_hostname(renderer):
    config = {"system": {"host_name": "rtr01"}}
    out = renderer.render(config)
    assert "hostname rtr01" in out


def test_render_defaults_hostname_when_absent(renderer):
    out = renderer.render({})
    assert "hostname nos" in out


def test_render_includes_frr_defaults(renderer):
    out = renderer.render({})
    assert "frr defaults traditional" in out


# ---------------------------------------------------------------------------
# IS-IS
# ---------------------------------------------------------------------------

def test_render_isis_interfaces(renderer):
    config = {
        "routing_options": {"router_id": "1.1.1.1"},
        "protocols": {
            "isis": {
                "interface": {
                    "eth0": {"point_to_point": True},
                    "lo0": {},
                }
            }
        },
    }
    out = renderer.render(config)
    assert "interface eth0" in out
    assert "isis network point-to-point" in out
    assert "interface lo0" in out


def test_render_isis_router_block(renderer):
    config = {
        "routing_options": {"router_id": "1.1.1.1"},
        "protocols": {
            "isis": {
                "interface": {"eth0": {}},
            }
        },
    }
    out = renderer.render(config)
    assert "router isis default" in out
    assert "net 49." in out


def test_render_no_isis_when_not_configured(renderer):
    out = renderer.render({})
    assert "router isis" not in out


# ---------------------------------------------------------------------------
# BGP
# ---------------------------------------------------------------------------

def test_render_bgp_block(renderer):
    config = {
        "routing_options": {"autonomous_system": 65000, "router_id": "1.1.1.1"},
        "protocols": {
            "bgp": {
                "group": {
                    "IBGP": {
                        "group_type": "internal",
                        "local_address": "1.1.1.1",
                        "neighbor": {"2.2.2.2": {}},
                    }
                }
            }
        },
    }
    out = renderer.render(config)
    assert "router bgp 65000" in out
    assert "bgp router-id 1.1.1.1" in out
    assert "neighbor IBGP peer-group" in out
    assert "neighbor 2.2.2.2 peer-group IBGP" in out


def test_render_no_bgp_when_not_configured(renderer):
    out = renderer.render({})
    assert "router bgp" not in out


def test_render_bgp_requires_asn(renderer):
    """BGP config without ASN should produce no router bgp stanza."""
    config = {
        "protocols": {
            "bgp": {
                "group": {
                    "IBGP": {"group_type": "internal", "neighbor": {"2.2.2.2": {}}}
                }
            }
        }
    }
    out = renderer.render(config)
    assert "router bgp" not in out


# ---------------------------------------------------------------------------
# Full router config (IS-IS + BGP together)
# ---------------------------------------------------------------------------

def test_render_full_router_config(renderer):
    config = {
        "system": {"host_name": "rtr01"},
        "routing_options": {
            "router_id": "1.1.1.1",
            "autonomous_system": 65000,
        },
        "protocols": {
            "isis": {
                "interface": {
                    "eth0": {"point_to_point": True},
                    "lo0": {},
                },
                "level_2": {"wide_metrics_only": True},
            },
            "bgp": {
                "group": {
                    "IBGP": {
                        "group_type": "internal",
                        "local_address": "1.1.1.1",
                        "neighbor": {"2.2.2.2": {"description": "rtr02"}},
                    }
                }
            },
        },
    }
    out = renderer.render(config)

    assert "hostname rtr01" in out
    assert "router isis default" in out
    assert "net 49." in out
    assert "metric-style wide" in out
    assert "router bgp 65000" in out
    assert "neighbor 2.2.2.2 description rtr02" in out


# ---------------------------------------------------------------------------
# family iso
# ---------------------------------------------------------------------------

def test_render_family_iso_loopback(renderer):
    """NET from family iso address goes into router isis block, not interface stanza."""
    config = {
        "interfaces": {
            "lo0": {
                "family_iso": {"address": "49.0001.0000.0101.0101.00"},
            }
        },
        "protocols": {"isis": {"interface": {"lo0.0": {}}}},
    }
    out = renderer.render(config)
    assert "interface lo0" in out
    assert "net 49.0001.0000.0101.0101.00" in out
    assert "router isis default" in out
    # iso address must NOT appear in interface stanza — it belongs in router isis
    assert "iso address" not in out
    assert "iso enable" not in out


def test_render_family_iso_from_unit(renderer):
    """NET address in unit 0 family iso is picked up for router isis block."""
    config = {
        "interfaces": {
            "lo0": {
                "unit": {
                    "0": {
                        "family_inet": {"address": {"1.1.1.1/32": {}}},
                        "family_iso": {"address": "49.0001.0000.0101.0101.00"},
                    }
                }
            },
            "et1": {"unit": {"0": {"family_iso": True}}},
        },
        "protocols": {
            "isis": {
                "interface": {
                    "et1.0": {"point_to_point": True},
                    "lo0.0": {"passive": True},
                }
            }
        },
    }
    out = renderer.render(config)
    assert "net 49.0001.0000.0101.0101.00" in out
    assert "ip address 1.1.1.1/32" in out
    assert "interface et1" in out
    assert "isis network point-to-point" in out
    assert "interface lo0" in out
    assert "isis passive" in out
    assert "iso address" not in out


def test_render_family_iso_included_in_isis_stanza(renderer):
    config = {
        "routing_options": {"router_id": "1.1.1.1"},
        "interfaces": {
            "lo0": {
                "family_iso": {"address": "49.0001.0000.0101.0101.00"},
                "family_inet": {"address": {"1.1.1.1/32": {}}},
            }
        },
        "protocols": {
            "isis": {
                "interface": {
                    "eth0": {"point_to_point": True},
                    "lo0.0": {},
                }
            }
        },
    }
    out = renderer.render(config)
    assert "net 49.0001.0000.0101.0101.00" in out
    assert "ip address 1.1.1.1/32" in out
    assert "router isis default" in out


def test_render_no_iso_when_not_configured(renderer):
    config = {
        "interfaces": {
            "lo0": {"family_inet": {"address": {"1.1.1.1/32": {}}}},
        }
    }
    out = renderer.render(config)
    assert "iso" not in out


# ---------------------------------------------------------------------------
# IS-IS interfaces with unit notation
# ---------------------------------------------------------------------------

def test_render_isis_interface_unit_notation(renderer):
    """et1.0 in protocols isis interface → frr.conf uses et1 (strip .0)."""
    config = {
        "routing_options": {"router_id": "1.1.1.1"},
        "interfaces": {
            "et1": {"family_inet": {"address": {"10.0.0.1/30": {}}}},
            "lo0": {"family_inet": {"address": {"1.1.1.1/32": {}}}},
        },
        "protocols": {
            "isis": {
                "interface": {
                    "et1.0": {"point_to_point": True},
                    "lo0.0": {},
                }
            }
        },
    }
    out = renderer.render(config)
    assert "interface et1" in out
    assert "interface lo0" in out
    assert "isis network point-to-point" in out
    # unit suffix must not appear in frr.conf
    assert "et1.0" not in out
    assert "lo0.0" not in out


def test_render_isis_multi_area_zone_with_unit_iface(renderer):
    """Multi-area zone NET goes to router isis block; unit-notation interfaces render correctly."""
    config = {
        "routing_options": {"router_id": "2.2.2.2"},
        "interfaces": {
            "et0": {"family_inet": {"address": {"10.1.1.1/30": {}}}},
            "lo0": {
                "family_iso": {"address": "49.0001.0002.0000.0202.0202.00"},
                "family_inet": {"address": {"2.2.2.2/32": {}}},
            },
        },
        "protocols": {
            "isis": {
                "interface": {
                    "et0.0": {"point_to_point": True},
                    "lo0.0": {},
                }
            }
        },
    }
    out = renderer.render(config)
    assert "interface et0" in out
    assert "interface lo0" in out
    assert "net 49.0001.0002.0000.0202.0202.00" in out
    assert "iso address" not in out
