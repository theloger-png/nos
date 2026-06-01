"""Unit tests for nos.drivers.frr.bgp.BGPGenerator."""
import pytest

from nos.drivers.frr.bgp import BGPGenerator


@pytest.fixture()
def gen():
    return BGPGenerator()


# ---------------------------------------------------------------------------
# No ASN → empty output
# ---------------------------------------------------------------------------

def test_render_without_asn_returns_empty(gen):
    lines = gen.render({}, asn=None)
    assert lines == []


# ---------------------------------------------------------------------------
# Basic structure
# ---------------------------------------------------------------------------

def test_render_router_bgp_header(gen):
    lines = gen.render({"group": {}}, asn=65000)
    text = "\n".join(lines)
    assert "router bgp 65000" in text


def test_render_router_id(gen):
    lines = gen.render({"group": {}}, asn=65000, router_id="1.1.1.1")
    text = "\n".join(lines)
    assert "bgp router-id 1.1.1.1" in text


def test_render_ends_with_bang(gen):
    lines = gen.render({"group": {}}, asn=65000)
    assert lines[-1] == "!"


# ---------------------------------------------------------------------------
# Peer groups
# ---------------------------------------------------------------------------

def test_render_ibgp_group(gen):
    bgp_cfg = {
        "group": {
            "IBGP": {
                "group_type": "internal",
                "local_as": 65000,
                "local_address": "1.1.1.1",
                "neighbor": {"2.2.2.2": {}},
            }
        }
    }
    lines = gen.render(bgp_cfg, asn=65000)
    text = "\n".join(lines)
    assert "neighbor IBGP peer-group" in text
    assert "neighbor IBGP remote-as 65000" in text
    assert "neighbor IBGP update-source 1.1.1.1" in text
    assert "neighbor 2.2.2.2 peer-group IBGP" in text


def test_render_ebgp_group(gen):
    bgp_cfg = {
        "group": {
            "EBGP": {
                "group_type": "external",
                "peer_as": 65001,
                "neighbor": {"10.0.0.2": {}},
            }
        }
    }
    lines = gen.render(bgp_cfg, asn=65000)
    text = "\n".join(lines)
    assert "neighbor EBGP remote-as 65001" in text


def test_render_neighbor_description(gen):
    bgp_cfg = {
        "group": {
            "IBGP": {
                "group_type": "internal",
                "neighbor": {"2.2.2.2": {"description": "peer-rtr02"}},
            }
        }
    }
    lines = gen.render(bgp_cfg, asn=65000)
    text = "\n".join(lines)
    assert "neighbor 2.2.2.2 description peer-rtr02" in text


def test_render_neighbor_auth_key(gen):
    bgp_cfg = {
        "group": {
            "IBGP": {
                "group_type": "internal",
                "neighbor": {"2.2.2.2": {"authentication_key": "secret"}},
            }
        }
    }
    lines = gen.render(bgp_cfg, asn=65000)
    text = "\n".join(lines)
    assert "neighbor 2.2.2.2 password secret" in text


# ---------------------------------------------------------------------------
# Address families
# ---------------------------------------------------------------------------

def test_render_ipv4_unicast_activated(gen):
    bgp_cfg = {
        "group": {
            "IBGP": {
                "group_type": "internal",
                "family_inet": {"unicast": True},
                "neighbor": {"2.2.2.2": {}},
            }
        }
    }
    lines = gen.render(bgp_cfg, asn=65000)
    text = "\n".join(lines)
    assert "address-family ipv4 unicast" in text
    assert "neighbor IBGP activate" in text
    assert "exit-address-family" in text


def test_render_ipv6_unicast_activated(gen):
    bgp_cfg = {
        "group": {
            "IBGP": {
                "group_type": "internal",
                "family_inet6": {"unicast": True},
                "neighbor": {"2::2": {}},
            }
        }
    }
    lines = gen.render(bgp_cfg, asn=65000)
    text = "\n".join(lines)
    assert "address-family ipv6 unicast" in text


def test_render_export_policy(gen):
    bgp_cfg = {
        "group": {
            "IBGP": {
                "group_type": "internal",
                "export": "EXPORT-ALL",
                "neighbor": {"2.2.2.2": {}},
            }
        }
    }
    lines = gen.render(bgp_cfg, asn=65000)
    text = "\n".join(lines)
    assert "route-map EXPORT-ALL out" in text


def test_render_import_policy(gen):
    bgp_cfg = {
        "group": {
            "IBGP": {
                "group_type": "internal",
                "import_policy": "IMPORT-ALL",
                "neighbor": {"2.2.2.2": {}},
            }
        }
    }
    lines = gen.render(bgp_cfg, asn=65000)
    text = "\n".join(lines)
    assert "route-map IMPORT-ALL in" in text
