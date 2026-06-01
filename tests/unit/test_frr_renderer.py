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
    assert "isis point-to-point" in out
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
