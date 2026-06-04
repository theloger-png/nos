"""Unit tests for BGP lo0 local-interface and router-id derivation."""
import pytest

from nos.drivers.frr.bgp import BGPGenerator
from nos.drivers.frr.renderer import FRRRenderer


@pytest.fixture()
def bgp():
    return BGPGenerator()


@pytest.fixture()
def renderer():
    return FRRRenderer()


def test_bgp_local_interface_renders_update_source(bgp):
    bgp_cfg = {
        "group": {
            "IBGP": {
                "group_type": "internal",
                "local_interface": "lo0",
                "neighbor": {"2.2.2.2": {}},
            }
        }
    }
    lines = bgp.render(bgp_cfg, asn=65000, router_id="10.0.0.1")
    text = "\n".join(lines)
    assert "neighbor IBGP update-source lo0" in text


def test_bgp_local_address_ip_renders_update_source(bgp):
    bgp_cfg = {
        "group": {
            "IBGP": {
                "group_type": "internal",
                "local_address": "10.0.0.1",
                "neighbor": {"2.2.2.2": {}},
            }
        }
    }
    lines = bgp.render(bgp_cfg, asn=65000, router_id="10.0.0.1")
    text = "\n".join(lines)
    assert "neighbor IBGP update-source 10.0.0.1" in text


def test_bgp_local_interface_takes_precedence_over_local_address(bgp):
    bgp_cfg = {
        "group": {
            "IBGP": {
                "group_type": "internal",
                "local_address": "10.0.0.1",
                "local_interface": "lo0",
                "neighbor": {"2.2.2.2": {}},
            }
        }
    }
    lines = bgp.render(bgp_cfg, asn=65000, router_id="10.0.0.1")
    text = "\n".join(lines)
    assert "neighbor IBGP update-source lo0" in text
    assert "neighbor IBGP update-source 10.0.0.1" not in text


def test_renderer_router_id_derived_from_lo0(renderer):
    config = {
        "interfaces": {
            "lo0": {"family_inet": {"address": {"192.168.1.1/32": {"primary": True}}}}
        },
        "routing_options": {"autonomous_system": 65001},
        "protocols": {
            "bgp": {
                "group": {
                    "IBGP": {
                        "group_type": "internal",
                        "local_interface": "lo0",
                        "neighbor": {"10.0.0.2": {}},
                    }
                }
            }
        },
    }
    out = renderer.render(config)
    assert "bgp router-id 192.168.1.1" in out
    assert "neighbor IBGP update-source lo0" in out


def test_renderer_no_router_id_without_lo0_or_explicit(renderer):
    config = {
        "routing_options": {"autonomous_system": 65001},
        "protocols": {
            "bgp": {
                "group": {
                    "IBGP": {
                        "group_type": "internal",
                        "neighbor": {"10.0.0.2": {}},
                    }
                }
            }
        },
    }
    out = renderer.render(config)
    assert "bgp router-id" not in out


def test_bgp_no_update_source_without_local_address_or_interface(bgp):
    bgp_cfg = {
        "group": {
            "IBGP": {
                "group_type": "internal",
                "neighbor": {"2.2.2.2": {}},
            }
        }
    }
    lines = bgp.render(bgp_cfg, asn=65000)
    text = "\n".join(lines)
    assert "update-source" not in text
