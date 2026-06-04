"""Tests for 'show route advertising-protocol / receive-protocol bgp <ip>'."""
from __future__ import annotations

import json
from unittest.mock import MagicMock

import pytest
from prompt_toolkit.completion import CompleteEvent
from prompt_toolkit.document import Document

from nos.cli.commands.show.route import (
    _frr_adj_fetch,
    _parse_bgp_adj_json,
    _render_adj_table,
    show_route,
    show_route_adj_protocol,
)
from nos.cli.completer import NOSCompleter
from nos.cli.parser import CLIMode


# ── FRR mock helpers ─────────────────────────────────────────────────────────

def _make_frr(ipv4_adv=None, ipv4_rcv=None, ipv6_adv=None, ipv6_rcv=None,
              neighbor_not_found=False):
    """Return a mock FRRClient for adj-rib commands."""
    frr = MagicMock()

    def _show(cmd: str) -> str:
        if neighbor_not_found:
            return "No such neighbor"   # non-JSON → triggers error path

        mapping: dict[str, object] = {}
        if ipv4_adv is not None:
            mapping[f"show ip bgp neighbors 10.0.0.2 advertised-routes json"] = ipv4_adv
        if ipv4_rcv is not None:
            mapping[f"show ip bgp neighbors 10.0.0.2 received-routes json"] = ipv4_rcv
        if ipv6_adv is not None:
            mapping[f"show bgp ipv6 unicast neighbors 10.0.0.2 advertised-routes json"] = ipv6_adv
        if ipv6_rcv is not None:
            mapping[f"show bgp ipv6 unicast neighbors 10.0.0.2 received-routes json"] = ipv6_rcv

        if cmd not in mapping:
            return json.dumps({})   # empty result for unregistered commands
        return json.dumps(mapping[cmd])

    frr.show.side_effect = _show
    return frr


_ADV_JSON = {
    "advertisedRoutes": {
        "10.0.0.0/24": {
            "nextHop": "0.0.0.0",
            "locPrf": 100,
            "metric": 0,
            "path": "",
            "origin": "IGP",
        },
        "172.18.4.40/29": {
            "nextHop": "0.0.0.0",
            "locPrf": 100,
            "metric": 0,
            "path": "",
            "origin": "IGP",
        },
        "192.168.100.0/24": {
            "nextHop": "0.0.0.0",
            "locPrf": 100,
            "metric": 0,
            "path": "",
            "origin": "IGP",
        },
    },
    "totalPrefixCounter": 3,
}

_RCV_JSON = {
    "receivedRoutes": {
        "10.1.0.0/24": {
            "nextHop": "10.0.0.2",
            "locPrf": 100,
            "metric": 0,
            "path": "",
            "origin": "IGP",
        },
        "192.168.200.0/24": {
            "nextHop": "10.0.0.2",
            "locPrf": 100,
            "metric": 0,
            "path": "",
            "origin": "IGP",
        },
    },
    "totalPrefixCounter": 2,
}


# ── _frr_adj_fetch ────────────────────────────────────────────────────────────

def test_frr_adj_fetch_success():
    frr = MagicMock()
    frr.show.return_value = json.dumps({"advertisedRoutes": {}})
    data, err = _frr_adj_fetch(frr, "show ip bgp neighbors 10.0.0.1 advertised-routes json")
    assert err is None
    assert data == {"advertisedRoutes": {}}


def test_frr_adj_fetch_non_json_neighbor_not_found():
    frr = MagicMock()
    frr.show.return_value = "No such neighbor or peer group is not found"
    data, err = _frr_adj_fetch(frr, "show ip bgp neighbors 10.0.0.1 advertised-routes json")
    assert data == {}
    assert err == "neighbor_not_found"


def test_frr_adj_fetch_exception():
    frr = MagicMock()
    frr.show.side_effect = RuntimeError("connection refused")
    data, err = _frr_adj_fetch(frr, "show ip bgp neighbors 10.0.0.1 advertised-routes json")
    assert data == {}
    assert err is not None


# ── _parse_bgp_adj_json ───────────────────────────────────────────────────────

def test_parse_bgp_adj_json_advertised():
    routes = _parse_bgp_adj_json(_ADV_JSON, "advertisedRoutes")
    assert len(routes) == 3
    prefixes = [r["prefix"] for r in routes]
    assert "10.0.0.0/24" in prefixes
    assert "172.18.4.40/29" in prefixes


def test_parse_bgp_adj_json_received():
    routes = _parse_bgp_adj_json(_RCV_JSON, "receivedRoutes")
    assert len(routes) == 2
    r = next(r for r in routes if r["prefix"] == "10.1.0.0/24")
    assert r["nexthop"] == "10.0.0.2"
    assert r["local_pref"] == 100
    assert r["as_path"] == "I"


def test_parse_bgp_adj_json_as_path_with_segments():
    data = {
        "advertisedRoutes": {
            "10.0.0.0/24": {
                "nextHop": "0.0.0.0",
                "path": "65001 65002",
                "origin": "IGP",
            }
        }
    }
    routes = _parse_bgp_adj_json(data, "advertisedRoutes")
    assert routes[0]["as_path"] == "65001 65002 I"


def test_parse_bgp_adj_json_origin_codes():
    for origin, code in [("IGP", "I"), ("EGP", "E"), ("incomplete", "?")]:
        data = {"advertisedRoutes": {"1.0.0.0/24": {"origin": origin}}}
        routes = _parse_bgp_adj_json(data, "advertisedRoutes")
        assert routes[0]["as_path"] == code


def test_parse_bgp_adj_json_sorted_by_prefix():
    data = {
        "advertisedRoutes": {
            "192.168.0.0/24": {"origin": "IGP"},
            "10.0.0.0/8":     {"origin": "IGP"},
            "172.16.0.0/12":  {"origin": "IGP"},
        }
    }
    routes = _parse_bgp_adj_json(data, "advertisedRoutes")
    prefixes = [r["prefix"] for r in routes]
    assert prefixes == sorted(prefixes, key=lambda p: (int(__import__("ipaddress").ip_network(p).network_address), 0))


def test_parse_bgp_adj_json_empty_key():
    routes = _parse_bgp_adj_json({}, "advertisedRoutes")
    assert routes == []


def test_parse_bgp_adj_json_med_zero_treated_as_blank():
    data = {"advertisedRoutes": {"10.0.0.0/24": {"metric": 0, "origin": "IGP"}}}
    routes = _parse_bgp_adj_json(data, "advertisedRoutes")
    assert routes[0]["med"] is None


def test_parse_bgp_adj_json_med_nonzero():
    data = {"advertisedRoutes": {"10.0.0.0/24": {"metric": 50, "origin": "IGP"}}}
    routes = _parse_bgp_adj_json(data, "advertisedRoutes")
    assert routes[0]["med"] == 50


# ── _render_adj_table ─────────────────────────────────────────────────────────

def test_render_adj_table_self_nexthop():
    routes = [{"prefix": "10.0.0.0/24", "nexthop": "0.0.0.0", "med": None, "local_pref": 100, "as_path": "I"}]
    output = _render_adj_table(routes, "inet.0")
    assert "Self" in output
    assert "10.0.0.0/24" in output
    assert "inet.0: 1 destinations" in output


def test_render_adj_table_real_nexthop():
    routes = [{"prefix": "10.1.0.0/24", "nexthop": "10.0.0.2", "med": None, "local_pref": 100, "as_path": "I"}]
    output = _render_adj_table(routes, "inet.0")
    assert "10.0.0.2" in output


def test_render_adj_table_header_row():
    output = _render_adj_table([], "inet.0")
    assert "Prefix" in output
    assert "Nexthop" in output
    assert "MED" in output
    assert "Lclpref" in output
    assert "AS path" in output


def test_render_adj_table_med_shown():
    routes = [{"prefix": "10.0.0.0/24", "nexthop": "0.0.0.0", "med": 50, "local_pref": 100, "as_path": "I"}]
    output = _render_adj_table(routes, "inet.0")
    assert "50" in output


def test_render_adj_table_ipv6_empty_nexthop_is_self():
    routes = [{"prefix": "2001:db8::/32", "nexthop": "::", "med": None, "local_pref": None, "as_path": "I"}]
    output = _render_adj_table(routes, "inet6.0")
    assert "Self" in output


# ── show_route_adj_protocol ───────────────────────────────────────────────────

def test_show_route_adj_frr_none():
    result = show_route_adj_protocol(["bgp", "10.0.0.2"], frr=None, advertised=True)
    assert result == "BGP is not running"


def test_show_route_adj_missing_args():
    frr = MagicMock()
    result = show_route_adj_protocol([], frr=frr, advertised=True)
    assert result.startswith("error:")


def test_show_route_adj_wrong_protocol():
    frr = MagicMock()
    result = show_route_adj_protocol(["ospf", "10.0.0.2"], frr=frr, advertised=True)
    assert result.startswith("error:")


def test_show_route_adj_invalid_ip():
    frr = MagicMock()
    result = show_route_adj_protocol(["bgp", "not-an-ip"], frr=frr, advertised=True)
    assert "invalid IP address" in result


def test_show_route_adj_neighbor_not_found():
    frr = _make_frr(neighbor_not_found=True)
    result = show_route_adj_protocol(["bgp", "10.0.0.2"], frr=frr, advertised=True)
    assert "not found" in result


def test_show_route_adj_advertised_output():
    frr = _make_frr(ipv4_adv=_ADV_JSON)
    result = show_route_adj_protocol(["bgp", "10.0.0.2"], frr=frr, advertised=True)
    assert "inet.0: 3 destinations" in result
    assert "10.0.0.0/24" in result
    assert "172.18.4.40/29" in result
    assert "192.168.100.0/24" in result
    assert "Self" in result


def test_show_route_adj_received_output():
    frr = _make_frr(ipv4_rcv=_RCV_JSON)
    result = show_route_adj_protocol(["bgp", "10.0.0.2"], frr=frr, advertised=False)
    assert "inet.0: 2 destinations" in result
    assert "10.1.0.0/24" in result
    assert "192.168.200.0/24" in result
    assert "10.0.0.2" in result


def test_show_route_adj_no_routes():
    frr = _make_frr(ipv4_adv={"advertisedRoutes": {}})
    result = show_route_adj_protocol(["bgp", "10.0.0.2"], frr=frr, advertised=True)
    assert "No routes advertised to" in result


def test_show_route_adj_both_families():
    ipv6_adv = {
        "advertisedRoutes": {
            "2001:db8::/32": {"nextHop": "::", "locPrf": 100, "metric": 0, "origin": "IGP"},
        },
        "totalPrefixCounter": 1,
    }
    frr = _make_frr(ipv4_adv=_ADV_JSON, ipv6_adv=ipv6_adv)
    result = show_route_adj_protocol(["bgp", "10.0.0.2"], frr=frr, advertised=True)
    assert "inet.0" in result
    assert "inet6.0" in result
    assert "2001:db8::/32" in result


# ── show_route dispatch ───────────────────────────────────────────────────────

def test_show_route_dispatches_advertising_protocol():
    frr = _make_frr(ipv4_adv=_ADV_JSON)
    result = show_route(["advertising-protocol", "bgp", "10.0.0.2"], frr=frr)
    assert "inet.0" in result
    assert "10.0.0.0/24" in result


def test_show_route_dispatches_receive_protocol():
    frr = _make_frr(ipv4_rcv=_RCV_JSON)
    result = show_route(["receive-protocol", "bgp", "10.0.0.2"], frr=frr)
    assert "inet.0" in result
    assert "10.1.0.0/24" in result


# ── Tab completion ────────────────────────────────────────────────────────────

def _completions(text: str) -> list[str]:
    completer = NOSCompleter(CLIMode.OPERATIONAL, [])
    doc = Document(text)
    event = CompleteEvent()
    return [c.text for c in completer.get_completions(doc, event)]


def test_complete_show_route_lists_adj_subcommands():
    completions = _completions("show route ")
    assert "advertising-protocol" in completions
    assert "receive-protocol" in completions


def test_complete_show_route_advertising_protocol_bgp():
    completions = _completions("show route advertising-protocol ")
    assert "bgp" in completions


def test_complete_show_route_receive_protocol_bgp():
    completions = _completions("show route receive-protocol ")
    assert "bgp" in completions


def test_complete_show_route_advertising_protocol_bgp_partial():
    completions = _completions("show route advertising-protocol b")
    assert "bgp" in completions


def test_complete_show_route_advertising_protocol_ip_hint():
    completions = _completions("show route advertising-protocol bgp ")
    assert "<neighbor-ip>" in completions


def test_complete_show_route_receive_protocol_ip_hint():
    completions = _completions("show route receive-protocol bgp ")
    assert "<neighbor-ip>" in completions


def test_complete_show_route_pipe_after_adj_command():
    completions = _completions("show route advertising-protocol bgp 10.0.0.2 ")
    assert "|" in completions
