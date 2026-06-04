"""Tests for BGP redistribute feature."""
import pytest

from nos.config.schema import BgpConfig, BgpFamilyInet, BgpFamilyInet6, NOSConfig
from nos.drivers.frr.bgp import BGPGenerator


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------

def test_bgp_family_inet_accepts_valid_protocols():
    fi = BgpFamilyInet(redistribute={"connected": True, "static": True})
    assert fi.redistribute == {"connected": True, "static": True}


def test_bgp_family_inet6_accepts_valid_protocols():
    fi6 = BgpFamilyInet6(redistribute={"isis": True, "ospf": True})
    assert fi6.redistribute == {"isis": True, "ospf": True}


def test_bgp_family_inet_rejects_unknown_protocol():
    with pytest.raises(Exception, match="Invalid redistribute protocol"):
        BgpFamilyInet(redistribute={"bogus": True})


def test_bgp_family_inet6_rejects_unknown_protocol():
    with pytest.raises(Exception, match="Invalid redistribute protocol"):
        BgpFamilyInet6(redistribute={"not-a-proto": True})


def test_bgp_family_inet_defaults_empty():
    fi = BgpFamilyInet()
    assert fi.redistribute == {}


def test_bgp_config_accepts_top_level_family_inet():
    cfg = BgpConfig(family_inet=BgpFamilyInet(redistribute={"connected": True}))
    assert cfg.family_inet is not None
    assert "connected" in cfg.family_inet.redistribute


def test_bgp_config_accepts_top_level_family_inet6():
    cfg = BgpConfig(family_inet6=BgpFamilyInet6(redistribute={"static": True}))
    assert cfg.family_inet6 is not None
    assert "static" in cfg.family_inet6.redistribute


def test_nos_config_validates_bgp_redistribute():
    config = {
        "protocols": {
            "bgp": {
                "family_inet": {"redistribute": {"connected": True}},
                "group": {},
            }
        },
        "routing_options": {"autonomous_system": 65000},
    }
    nos = NOSConfig.model_validate(config)
    assert nos.protocols.bgp.family_inet.redistribute == {"connected": True}


# ---------------------------------------------------------------------------
# FRR renderer — IPv4
# ---------------------------------------------------------------------------

@pytest.fixture()
def gen():
    return BGPGenerator()


def test_redistribute_ipv4_emitted_before_activate(gen):
    bgp_cfg = {
        "family_inet": {"redistribute": {"connected": True}},
        "group": {
            "IBGP": {
                "group_type": "internal",
                "neighbor": {"2.2.2.2": {}},
            }
        },
    }
    lines = gen.render(bgp_cfg, asn=65000)
    text = "\n".join(lines)
    assert " address-family ipv4 unicast" in text
    assert "  redistribute connected" in text
    # redistribute must appear before activate
    redist_idx = next(i for i, l in enumerate(lines) if "redistribute connected" in l)
    activate_idx = next(i for i, l in enumerate(lines) if "neighbor IBGP activate" in l)
    assert redist_idx < activate_idx


def test_redistribute_multiple_ipv4_protocols(gen):
    bgp_cfg = {
        "family_inet": {"redistribute": {"connected": True, "static": True, "isis": True}},
        "group": {"G": {"group_type": "internal", "neighbor": {"1.1.1.1": {}}}},
    }
    text = "\n".join(gen.render(bgp_cfg, asn=65000))
    assert "  redistribute connected" in text
    assert "  redistribute static" in text
    assert "  redistribute isis" in text


def test_redistribute_ipv4_not_repeated_for_second_group(gen):
    bgp_cfg = {
        "family_inet": {"redistribute": {"connected": True}},
        "group": {
            "G1": {"group_type": "internal", "neighbor": {"1.1.1.1": {}}},
            "G2": {"group_type": "internal", "neighbor": {"2.2.2.2": {}}},
        },
    }
    lines = gen.render(bgp_cfg, asn=65000)
    redist_count = sum(1 for l in lines if "redistribute connected" in l)
    assert redist_count == 1


def test_no_redistribute_when_not_configured(gen):
    bgp_cfg = {
        "group": {
            "IBGP": {"group_type": "internal", "neighbor": {"2.2.2.2": {}}},
        },
    }
    text = "\n".join(gen.render(bgp_cfg, asn=65000))
    assert "redistribute" not in text


def test_empty_redistribute_dict_emits_no_lines(gen):
    bgp_cfg = {
        "family_inet": {"redistribute": {}},
        "group": {"G": {"group_type": "internal", "neighbor": {"1.1.1.1": {}}}},
    }
    text = "\n".join(gen.render(bgp_cfg, asn=65000))
    assert "redistribute" not in text


# ---------------------------------------------------------------------------
# FRR renderer — IPv6
# ---------------------------------------------------------------------------

def test_redistribute_ipv6_emitted_before_activate(gen):
    bgp_cfg = {
        "family_inet6": {"redistribute": {"static": True}},
        "group": {
            "IBGP": {
                "group_type": "internal",
                "family_inet6": {"unicast": True},
                "neighbor": {"2::2": {}},
            }
        },
    }
    lines = gen.render(bgp_cfg, asn=65000)
    text = "\n".join(lines)
    assert " address-family ipv6 unicast" in text
    assert "  redistribute static" in text
    redist_idx = next(i for i, l in enumerate(lines) if "redistribute static" in l)
    activate_idx = next(i for i, l in enumerate(lines) if "neighbor IBGP activate" in l)
    assert redist_idx < activate_idx


def test_redistribute_ipv6_not_repeated_for_second_group(gen):
    bgp_cfg = {
        "family_inet6": {"redistribute": {"connected": True}},
        "group": {
            "G1": {
                "group_type": "internal",
                "family_inet6": {"unicast": True},
                "neighbor": {"1::1": {}},
            },
            "G2": {
                "group_type": "internal",
                "family_inet6": {"unicast": True},
                "neighbor": {"2::2": {}},
            },
        },
    }
    lines = gen.render(bgp_cfg, asn=65000)
    redist_count = sum(1 for l in lines if "redistribute connected" in l)
    assert redist_count == 1


def test_redistribute_both_families(gen):
    """Group with both families explicit: redistributes appear in both AF blocks."""
    bgp_cfg = {
        "family_inet": {"redistribute": {"connected": True}},
        "family_inet6": {"redistribute": {"static": True}},
        "group": {
            "DUAL": {
                "group_type": "internal",
                "family_inet": {"unicast": True},
                "family_inet6": {"unicast": True},
                "neighbor": {"1.1.1.1": {}, "1::1": {}},
            }
        },
    }
    text = "\n".join(gen.render(bgp_cfg, asn=65000))
    assert "  redistribute connected" in text
    assert "  redistribute static" in text


# ---------------------------------------------------------------------------
# CLI completer
# ---------------------------------------------------------------------------

from nos.cli.completer import build_config_tree, navigate_tree


def test_completer_tree_has_bgp_family_inet_redistribute():
    root = build_config_tree()
    node = navigate_tree(root, ["protocols", "bgp", "family", "inet", "redistribute"])
    assert node is not None
    assert "connected" in node.children
    assert "static" in node.children
    assert "kernel" in node.children
    assert "isis" in node.children
    assert "ospf" in node.children
    assert "rip" in node.children


def test_completer_tree_has_bgp_family_inet6_redistribute():
    root = build_config_tree()
    node = navigate_tree(root, ["protocols", "bgp", "family", "inet6", "redistribute"])
    assert node is not None
    assert "connected" in node.children


def test_completer_redistribute_nodes_are_presence():
    root = build_config_tree()
    node = navigate_tree(root, ["protocols", "bgp", "family", "inet", "redistribute", "connected"])
    assert node is not None
    assert node.is_presence
