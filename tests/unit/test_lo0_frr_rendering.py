"""Unit tests for lo0 loopback interface rendering in FRRRenderer."""
import pytest

from nos.drivers.frr.renderer import FRRRenderer


@pytest.fixture()
def renderer():
    return FRRRenderer()


def test_lo0_ip_address_rendered(renderer):
    config = {
        "interfaces": {
            "lo0": {"family_inet": {"address": {"10.0.0.1/32": {"primary": True}}}}
        }
    }
    out = renderer.render(config)
    assert "interface lo0" in out
    assert "ip address 10.0.0.1/32" in out


def test_lo0_ipv6_address_rendered(renderer):
    config = {
        "interfaces": {
            "lo0": {"family_inet6": {"address": {"2001:db8::1/128": {"primary": True}}}}
        }
    }
    out = renderer.render(config)
    assert "interface lo0" in out
    assert "ipv6 address 2001:db8::1/128" in out


def test_lo0_router_id_derived_from_ip(renderer):
    config = {
        "interfaces": {
            "lo0": {"family_inet": {"address": {"10.0.0.1/32": {"primary": True}}}}
        },
        "routing_options": {"autonomous_system": 65000},
        "protocols": {
            "bgp": {
                "group": {
                    "IBGP": {"group_type": "internal", "neighbor": {"2.2.2.2": {}}}
                }
            }
        },
    }
    out = renderer.render(config)
    assert "bgp router-id 10.0.0.1" in out


def test_lo0_explicit_router_id_takes_precedence(renderer):
    config = {
        "interfaces": {
            "lo0": {"family_inet": {"address": {"10.0.0.1/32": {"primary": True}}}}
        },
        "routing_options": {"router_id": "2.2.2.2", "autonomous_system": 65000},
        "protocols": {
            "bgp": {
                "group": {
                    "IBGP": {"group_type": "internal", "neighbor": {"3.3.3.3": {}}}
                }
            }
        },
    }
    out = renderer.render(config)
    assert "bgp router-id 2.2.2.2" in out
    assert "bgp router-id 10.0.0.1" not in out


def test_lo0_combined_isis_and_ip_in_single_stanza(renderer):
    config = {
        "interfaces": {
            "lo0": {"family_inet": {"address": {"10.0.0.1/32": {"primary": True}}}}
        },
        "routing_options": {"router_id": "10.0.0.1"},
        "protocols": {
            "isis": {"interface": {"lo0": {}}}
        },
    }
    out = renderer.render(config)
    assert out.count("interface lo0") == 1
    assert "ip address 10.0.0.1/32" in out
    assert "ip router isis default" in out
    assert "isis passive" in out


def test_interface_without_ips_not_rendered_standalone(renderer):
    config = {
        "interfaces": {
            "eth0": {"description": "uplink"}
        }
    }
    out = renderer.render(config)
    assert "interface eth0" not in out


def test_lo0_isis_derived_router_id_used_for_net(renderer):
    config = {
        "interfaces": {
            "lo0": {"family_inet": {"address": {"1.1.1.1/32": {"primary": True}}}}
        },
        "protocols": {
            "isis": {"interface": {"lo0": {}}}
        },
    }
    out = renderer.render(config)
    assert "net 49.0001.0000.0101.0101.00" in out
