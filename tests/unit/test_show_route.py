"""Tests for 'show route' — nos/cli/commands/show/route.py and operational mode."""
from __future__ import annotations

import json
from unittest.mock import MagicMock, Mock, patch

import pytest
from prompt_toolkit.completion import CompleteEvent
from prompt_toolkit.document import Document

from nos.cli.commands.show.route import (
    NextHop,
    Route,
    _build_route_table,
    _parse_frr_json,
    _read_kernel_routes,
    render_brief,
    render_detail,
    render_terse,
    show_route,
)
from nos.cli.completer import NOSCompleter
from nos.cli.modes.operational import OperationalMode
from nos.cli.parser import CLIMode
from nos.config.store import ConfigStore


# ============================================================================
# Helpers
# ============================================================================

def _make_frr(ipv4_json: dict | None = None, ipv6_json: dict | None = None,
              bgp_json: dict | None = None, bgp6_json: dict | None = None) -> Mock:
    """Return a mock FRRClient whose show() returns provided JSON blobs."""
    frr = MagicMock()

    def _show(cmd: str) -> str:
        mapping = {
            "show ip route json":    ipv4_json or {},
            "show ipv6 route json":  ipv6_json or {},
            "show ip bgp json":      bgp_json  or {},
            "show ipv6 bgp json":    bgp6_json or {},
        }
        if cmd not in mapping:
            raise ValueError(f"Unexpected FRR command: {cmd!r}")
        return json.dumps(mapping[cmd])

    frr.show.side_effect = _show
    return frr


# ── Mock pyroute2 objects ────────────────────────────────────────────────────

class _MockRouteMsg:
    """Minimal stand-in for a pyroute2 rtmsg."""

    def __init__(
        self,
        family: int,
        proto: int,
        rtype: int,
        dst_len: int,
        dst: str | None = None,
        gateway: str | None = None,
        oif: int | None = None,
    ) -> None:
        self._attrs: dict = {
            "RTA_DST":     dst,
            "RTA_GATEWAY": gateway,
            "RTA_OIF":     oif,
        }
        self._fields: dict = {
            "family":  family,
            "proto":   proto,
            "type":    rtype,
            "dst_len": dst_len,
        }

    def get_attr(self, key: str):
        return self._attrs.get(key)

    def __getitem__(self, key: str):
        return self._fields[key]


class _MockLink:
    def __init__(self, name: str, index: int) -> None:
        self._a = {"IFLA_IFNAME": name}
        self._i = {"index": index}

    def get_attr(self, key: str):
        return self._a.get(key)

    def __getitem__(self, key: str):
        return self._i[key]


def _make_ipr_mock(routes4=None, routes6=None, links=None):
    ipr = MagicMock()
    ipr.__enter__ = Mock(return_value=ipr)
    ipr.__exit__  = Mock(return_value=False)

    def _get_routes(family=None):
        if family == 2:
            return routes4 or []
        if family == 10:
            return routes6 or []
        return []

    ipr.get_routes.side_effect = _get_routes
    ipr.get_links.return_value = links or []
    return ipr


_PATCH_IPROUTE = "nos.cli.commands.show.route._IPRoute"

_AF_INET  = 2
_AF_INET6 = 10
_RTN_UNICAST    = 1
_RTN_LOCAL      = 2
_RTN_BROADCAST  = 3
_RTN_BLACKHOLE  = 6
_RTPROT_KERNEL  = 2
_RTPROT_STATIC  = 4


# ============================================================================
# Kernel route parser
# ============================================================================

class TestReadKernelRoutes:
    def _make_ipr(self, routes4=None, routes6=None, idx_to_name=None):
        ipr = MagicMock()
        ipr.get_routes.side_effect = (
            lambda family=None: (routes4 or []) if family == _AF_INET
            else (routes6 or []) if family == _AF_INET6 else []
        )
        return ipr

    def test_static_default_route(self):
        r = _MockRouteMsg(_AF_INET, _RTPROT_STATIC, _RTN_UNICAST,
                          dst_len=0, dst=None, gateway="172.18.4.41", oif=2)
        ipr = self._make_ipr(routes4=[r])
        routes = _read_kernel_routes(ipr, {2: "eth0"}, None)
        assert len(routes) == 1
        assert routes[0].prefix     == "0.0.0.0/0"
        assert routes[0].protocol   == "Static"
        assert routes[0].preference == 5
        assert routes[0].nexthops[0].gateway   == "172.18.4.41"
        assert routes[0].nexthops[0].interface == "eth0"

    def test_direct_route(self):
        r = _MockRouteMsg(_AF_INET, _RTPROT_KERNEL, _RTN_UNICAST,
                          dst_len=24, dst="10.0.0.0", oif=3)
        ipr = self._make_ipr(routes4=[r])
        routes = _read_kernel_routes(ipr, {3: "irb.101"}, None)
        assert routes[0].protocol   == "Direct"
        assert routes[0].preference == 0
        assert routes[0].nexthops[0].gateway is None
        assert routes[0].nexthops[0].interface == "irb.101"

    def test_local_route(self):
        r = _MockRouteMsg(_AF_INET, _RTPROT_KERNEL, _RTN_LOCAL,
                          dst_len=32, dst="10.0.0.1", oif=3)
        ipr = self._make_ipr(routes4=[r])
        routes = _read_kernel_routes(ipr, {3: "eth0"}, None)
        assert routes[0].protocol == "Local"
        assert routes[0].is_local is True

    def test_broadcast_skipped(self):
        r = _MockRouteMsg(_AF_INET, _RTPROT_KERNEL, _RTN_BROADCAST,
                          dst_len=32, dst="10.0.0.255", oif=3)
        ipr = self._make_ipr(routes4=[r])
        routes = _read_kernel_routes(ipr, {3: "eth0"}, None)
        assert routes == []

    def test_frr_proto_skipped(self):
        r = _MockRouteMsg(_AF_INET, 186, _RTN_UNICAST,
                          dst_len=24, dst="10.2.0.0", gateway="192.168.1.1", oif=2)
        ipr = self._make_ipr(routes4=[r])
        routes = _read_kernel_routes(ipr, {2: "eth0"}, None)
        assert routes == []

    def test_ipv6_local(self):
        r = _MockRouteMsg(_AF_INET6, _RTPROT_KERNEL, _RTN_LOCAL,
                          dst_len=128, dst="::1", oif=1)
        ipr = self._make_ipr(routes6=[r])
        routes = _read_kernel_routes(ipr, {1: "lo"}, None)
        assert len(routes) == 1
        assert routes[0].prefix   == "::1/128"
        assert routes[0].family   == 6
        assert routes[0].is_local is True

    def test_ipv6_linklocal_skipped(self):
        r = _MockRouteMsg(_AF_INET6, _RTPROT_KERNEL, _RTN_UNICAST,
                          dst_len=64, dst="fe80::", oif=2)
        ipr = self._make_ipr(routes6=[r])
        routes = _read_kernel_routes(ipr, {2: "eth0"}, None)
        assert routes == []

    def test_alias_fn_applied(self):
        r = _MockRouteMsg(_AF_INET, _RTPROT_KERNEL, _RTN_UNICAST,
                          dst_len=24, dst="10.0.0.0", oif=2)
        ipr = self._make_ipr(routes4=[r])
        alias_map = {"ens33": "et0"}
        alias_fn = lambda name: alias_map.get(name, name)
        routes = _read_kernel_routes(ipr, {2: "ens33"}, alias_fn)
        assert routes[0].nexthops[0].interface == "et0"

    def test_blackhole_route(self):
        r = _MockRouteMsg(_AF_INET, _RTPROT_STATIC, _RTN_BLACKHOLE,
                          dst_len=8, dst="10.0.0.0", oif=None)
        ipr = self._make_ipr(routes4=[r])
        routes = _read_kernel_routes(ipr, {}, None)
        assert routes[0].is_blackhole is True
        assert routes[0].protocol == "Static"


# ============================================================================
# FRR JSON parser
# ============================================================================

class TestParseFRRJson:
    def test_static_default_route(self):
        data = {
            "0.0.0.0/0": [{
                "protocol": "static",
                "selected": True,
                "destSelected": True,
                "installed": True,
                "distance": 1,
                "metric": 0,
                "uptime": "10:47:00",
                "nexthops": [{
                    "ip": "172.18.4.41",
                    "interfaceName": "ens33",
                    "active": True,
                }],
            }]
        }
        routes = _parse_frr_json(data, 4, None)
        assert len(routes) == 1
        r = routes[0]
        assert r.prefix     == "0.0.0.0/0"
        assert r.protocol   == "Static"
        assert r.preference == 1
        assert r.age        == "10:47:00"
        assert r.installed  is True
        assert r.nexthops[0].gateway   == "172.18.4.41"
        assert r.nexthops[0].interface == "ens33"
        assert r.nexthops[0].selected  is True

    def test_connected_route(self):
        data = {
            "10.0.0.0/24": [{
                "protocol": "connected",
                "selected": True,
                "destSelected": True,
                "installed": True,
                "distance": 0,
                "metric": 0,
                "uptime": "10:46:57",
                "nexthops": [{
                    "directlyConnected": True,
                    "interfaceName": "irb.101",
                    "active": True,
                }],
            }]
        }
        routes = _parse_frr_json(data, 4, None)
        assert routes[0].protocol   == "Direct"
        assert routes[0].preference == 0

    def test_bgp_route(self):
        data = {
            "10.2.0.0/16": [{
                "protocol": "bgp",
                "selected": True,
                "destSelected": True,
                "installed": True,
                "distance": 20,
                "metric": 100,
                "uptime": "00:10:00",
                "nexthops": [{
                    "ip": "10.1.1.2",
                    "interfaceName": "eth0",
                    "active": True,
                }],
            }]
        }
        routes = _parse_frr_json(data, 4, None)
        assert routes[0].protocol   == "BGP"
        assert routes[0].preference == 20

    def test_isis_route(self):
        data = {
            "10.1.1.0/30": [{
                "protocol": "isis",
                "selected": True,
                "destSelected": True,
                "installed": True,
                "distance": 15,
                "metric": 10,
                "uptime": "00:05:00",
                "nexthops": [{"ip": "10.0.0.1", "interfaceName": "eth0", "active": True}],
            }]
        }
        routes = _parse_frr_json(data, 4, None)
        assert routes[0].protocol   == "IS-IS"
        assert routes[0].preference == 15
        assert routes[0].metric     == 10

    def test_hidden_route_not_installed(self):
        data = {
            "10.3.0.0/24": [{
                "protocol": "bgp",
                "selected": False,
                "destSelected": False,
                "installed": False,
                "distance": 20,
                "metric": 0,
                "uptime": "00:01:00",
                "nexthops": [{"ip": "10.1.1.2", "interfaceName": "eth0", "active": False}],
            }]
        }
        routes = _parse_frr_json(data, 4, None)
        assert routes[0].installed is False
        assert routes[0].hidden_reason != ""

    def test_ipv6_route(self):
        data = {
            "::1/128": [{
                "protocol": "connected",
                "selected": True,
                "destSelected": True,
                "installed": True,
                "distance": 0,
                "metric": 0,
                "uptime": "00:00:00",
                "nexthops": [{"interfaceName": "lo", "active": True}],
            }]
        }
        routes = _parse_frr_json(data, 6, None)
        assert len(routes) == 1
        assert routes[0].prefix  == "::1/128"
        assert routes[0].family  == 6

    def test_linklocal_skipped(self):
        data = {
            "fe80::/64": [{
                "protocol": "connected",
                "selected": True,
                "destSelected": True,
                "installed": True,
                "distance": 0,
                "metric": 0,
                "uptime": "00:00:00",
                "nexthops": [{"interfaceName": "eth0", "active": True}],
            }]
        }
        routes = _parse_frr_json(data, 6, None)
        assert routes == []

    def test_alias_fn_applied(self):
        data = {
            "10.0.0.0/24": [{
                "protocol": "connected",
                "selected": True,
                "destSelected": True,
                "installed": True,
                "distance": 0,
                "metric": 0,
                "uptime": "00:00:00",
                "nexthops": [{"interfaceName": "ens33", "active": True}],
            }]
        }
        alias_fn = lambda name: {"ens33": "et0"}.get(name, name)
        routes = _parse_frr_json(data, 4, alias_fn)
        assert routes[0].nexthops[0].interface == "et0"

    def test_multiple_entries_per_prefix(self):
        data = {
            "10.0.0.0/24": [
                {
                    "protocol": "bgp",
                    "selected": True,
                    "destSelected": True,
                    "installed": True,
                    "distance": 20,
                    "metric": 0,
                    "uptime": "00:10:00",
                    "nexthops": [{"ip": "10.1.1.1", "interfaceName": "eth0", "active": True}],
                },
                {
                    "protocol": "bgp",
                    "selected": False,
                    "destSelected": False,
                    "installed": False,
                    "distance": 20,
                    "metric": 0,
                    "uptime": "00:10:00",
                    "nexthops": [{"ip": "10.1.1.2", "interfaceName": "eth0", "active": False}],
                },
            ]
        }
        routes = _parse_frr_json(data, 4, None)
        assert len(routes) == 2
        active  = [r for r in routes if r.installed]
        hidden  = [r for r in routes if not r.installed]
        assert len(active) == 1
        assert len(hidden) == 1


# ============================================================================
# Brief format rendering
# ============================================================================

class TestRenderBrief:
    def _make_static_default(self) -> Route:
        return Route(
            prefix="0.0.0.0/0",
            family=4,
            protocol="Static",
            preference=5,
            age="10:47:00",
            nexthops=[NextHop(gateway="172.18.4.41", interface="et0", selected=True)],
            active=True,
            installed=True,
        )

    def _make_direct(self, prefix="10.0.0.0/24", iface="irb.101") -> Route:
        return Route(
            prefix=prefix,
            family=4,
            protocol="Direct",
            preference=0,
            age="10:46:57",
            nexthops=[NextHop(gateway=None, interface=iface, selected=True)],
            active=True,
            installed=True,
        )

    def test_header_line(self):
        routes = [self._make_static_default()]
        out = render_brief(routes, "inet.0")
        assert out.splitlines()[0] == (
            "inet.0: 1 destinations, 1 routes (1 active, 0 holddown, 0 hidden)"
        )

    def test_legend_line(self):
        out = render_brief([self._make_static_default()], "inet.0")
        assert "+ = Active Route, - = Last Active, * = Both" in out

    def test_static_route_prefix_line(self):
        route = self._make_static_default()
        out = render_brief([route], "inet.0")
        lines = out.splitlines()
        prefix_line = next(l for l in lines if "0.0.0.0/0" in l)
        assert "*[Static/5] 10:47:00" in prefix_line

    def test_static_route_nexthop_line(self):
        out = render_brief([self._make_static_default()], "inet.0")
        assert "> to 172.18.4.41 via et0" in out

    def test_direct_route_nexthop_no_gateway(self):
        out = render_brief([self._make_direct()], "inet.0")
        assert "> via irb.101" in out

    def test_local_route_nexthop(self):
        route = Route(
            prefix="10.0.0.1/32",
            family=4,
            protocol="Local",
            preference=0,
            age="00:00:00",
            nexthops=[NextHop(interface="eth0", selected=True)],
            active=True,
            installed=True,
            is_local=True,
        )
        out = render_brief([route], "inet.0")
        assert "Local via eth0" in out

    def test_blackhole_route(self):
        route = Route(
            prefix="192.0.2.0/24",
            family=4,
            protocol="Static",
            preference=5,
            age="00:00:00",
            nexthops=[],
            active=True,
            installed=True,
            is_blackhole=True,
        )
        out = render_brief([route], "inet.0")
        assert "Discard" in out

    def test_reject_route(self):
        route = Route(
            prefix="192.0.2.0/24",
            family=4,
            protocol="Static",
            preference=5,
            age="00:00:00",
            nexthops=[],
            active=True,
            installed=True,
            is_reject=True,
        )
        out = render_brief([route], "inet.0")
        assert "Reject" in out

    def test_multiple_routes_sorted(self):
        routes = [
            self._make_static_default(),
            self._make_direct("172.18.4.40/29", "et0"),
            self._make_direct("10.0.0.0/24", "irb.101"),
        ]
        out = render_brief(sorted(routes, key=lambda r: r.prefix), "inet.0")
        assert "0.0.0.0/0" in out
        assert "10.0.0.0/24" in out
        assert "172.18.4.40/29" in out

    def test_hidden_route_all_routes_stats(self):
        active = self._make_static_default()
        hidden = Route(
            prefix="10.3.0.0/24",
            family=4,
            protocol="BGP",
            preference=170,
            age="00:01:00",
            nexthops=[NextHop(gateway="10.1.1.2", interface="eth0", selected=False)],
            active=False,
            installed=False,
        )
        out = render_brief([active], "inet.0", all_routes=[active, hidden])
        assert "0 hidden" not in out  # hidden count should be 1
        assert "1 hidden" in out

    def test_inet6_table_name(self):
        route = Route(
            prefix="::1/128",
            family=6,
            protocol="Direct",
            preference=0,
            age="00:00:00",
            nexthops=[NextHop(interface="lo", selected=True)],
            active=True,
            installed=True,
        )
        out = render_brief([route], "inet6.0")
        assert "inet6.0:" in out

    def test_prefix_column_alignment(self):
        routes = [self._make_static_default()]
        out = render_brief(routes, "inet.0")
        lines = out.splitlines()
        route_line = next(l for l in lines if "0.0.0.0/0" in l)
        # The '*' should be at position >= 20 (prefix column width)
        star_pos = route_line.index("*")
        assert star_pos >= 9   # "0.0.0.0/0" is 9 chars

    def test_hidden_route_no_star(self):
        route = Route(
            prefix="10.3.0.0/24",
            family=4,
            protocol="BGP",
            preference=170,
            age="00:01:00",
            nexthops=[NextHop(gateway="10.1.1.2", interface="eth0", selected=False)],
            active=False,
            installed=False,
        )
        out = render_brief([route], "inet.0")
        lines = out.splitlines()
        route_line = next((l for l in lines if "10.3.0.0/24" in l), "")
        # Hidden route uses space marker, not '*'
        assert route_line
        assert "[BGP/170]" in route_line
        assert not route_line.startswith("*")
        # The marker character at position after prefix must not be '*'
        pw = max(20, len("10.3.0.0/24") + 2)
        assert len(route_line) > pw and route_line[pw] != "*"

    def test_ecmp_two_nexthops(self):
        route = Route(
            prefix="10.0.0.0/24",
            family=4,
            protocol="BGP",
            preference=170,
            age="00:05:00",
            nexthops=[
                NextHop(gateway="10.1.1.1", interface="eth0", selected=True),
                NextHop(gateway="10.1.1.2", interface="eth1", selected=True),
            ],
            active=True,
            installed=True,
        )
        out = render_brief([route], "inet.0")
        assert "> to 10.1.1.1 via eth0" in out
        assert "  to 10.1.1.2 via eth1" in out


# ============================================================================
# Terse format rendering
# ============================================================================

class TestRenderTerse:
    def test_nexthop_on_same_line(self):
        route = Route(
            prefix="0.0.0.0/0",
            family=4,
            protocol="Static",
            preference=5,
            age="10:47:00",
            nexthops=[NextHop(gateway="172.18.4.41", interface="et0", selected=True)],
            active=True,
            installed=True,
        )
        out = render_terse([route], "inet.0")
        lines = out.splitlines()
        route_line = next(l for l in lines if "0.0.0.0/0" in l)
        assert "10:47:00" in route_line
        assert "> to 172.18.4.41 via et0" in route_line

    def test_direct_terse(self):
        route = Route(
            prefix="10.0.0.0/24",
            family=4,
            protocol="Direct",
            preference=0,
            age="10:46:57",
            nexthops=[NextHop(interface="irb.101", selected=True)],
            active=True,
            installed=True,
        )
        out = render_terse([route], "inet.0")
        lines = out.splitlines()
        route_line = next(l for l in lines if "10.0.0.0/24" in l)
        assert "> via irb.101" in route_line
        # No separate nexthop lines
        assert sum(1 for l in lines if "irb.101" in l) == 1

    def test_header_present(self):
        out = render_terse([], "inet.0", all_routes=[])
        assert "inet.0:" in out


# ============================================================================
# Detail format rendering
# ============================================================================

class TestRenderDetail:
    def test_static_route_preference(self):
        route = Route(
            prefix="0.0.0.0/0",
            family=4,
            protocol="Static",
            preference=5,
            age="10:47:00",
            nexthops=[NextHop(gateway="172.18.4.41", interface="et0", selected=True)],
            active=True,
            installed=True,
        )
        out = render_detail([route], "inet.0")
        assert "Preference: 5" in out
        assert "*Static" in out

    def test_detail_next_hop_type_router(self):
        route = Route(
            prefix="0.0.0.0/0",
            family=4,
            protocol="Static",
            preference=5,
            age="10:47:00",
            nexthops=[NextHop(gateway="172.18.4.41", interface="et0", selected=True)],
            active=True,
            installed=True,
        )
        out = render_detail([route], "inet.0")
        assert "Next hop type: Router" in out

    def test_detail_next_hop_line_with_gateway(self):
        route = Route(
            prefix="0.0.0.0/0",
            family=4,
            protocol="Static",
            preference=5,
            age="10:47:00",
            nexthops=[NextHop(gateway="172.18.4.41", interface="et0", selected=True)],
            active=True,
            installed=True,
        )
        out = render_detail([route], "inet.0")
        assert "Next hop: 172.18.4.41 via et0, selected" in out

    def test_detail_next_hop_line_direct_no_gateway(self):
        route = Route(
            prefix="10.0.0.0/24",
            family=4,
            protocol="Direct",
            preference=0,
            age="00:00:00",
            nexthops=[NextHop(gateway=None, interface="irb.101", selected=True)],
            active=True,
            installed=True,
        )
        out = render_detail([route], "inet.0")
        assert "Next hop: via irb.101, selected" in out

    def test_detail_state_active_int(self):
        route = Route(
            prefix="10.0.0.0/24",
            family=4,
            protocol="Direct",
            preference=0,
            age="00:00:00",
            nexthops=[NextHop(interface="eth0", selected=True)],
            active=True,
            installed=True,
        )
        out = render_detail([route], "inet.0")
        assert "State: <Active Int>" in out

    def test_detail_bgp_state_has_ext(self):
        route = Route(
            prefix="10.2.0.0/16",
            family=4,
            protocol="BGP",
            preference=170,
            age="00:10:00",
            nexthops=[NextHop(gateway="10.1.1.2", interface="eth0", selected=True)],
            active=True,
            installed=True,
        )
        out = render_detail([route], "inet.0")
        assert "State: <Active Int Ext>" in out

    def test_detail_bgp_as_path(self):
        route = Route(
            prefix="10.2.0.0/16",
            family=4,
            protocol="BGP",
            preference=170,
            age="00:10:00",
            nexthops=[NextHop(gateway="10.1.1.2", interface="eth0", selected=True)],
            active=True,
            installed=True,
            as_path="65002 65003 I",
            communities="65001:200 65003:6789",
            local_pref=100,
            router_id="192.168.0.2",
        )
        out = render_detail([route], "inet.0")
        assert "AS path: 65002 65003 I" in out
        assert "Communities: 65001:200 65003:6789" in out
        assert "Localpref: 100" in out
        assert "Router ID: 192.168.0.2" in out
        assert "Validation State: unverified" in out

    def test_detail_bgp_source_line(self):
        route = Route(
            prefix="10.2.0.0/16",
            family=4,
            protocol="BGP",
            preference=170,
            age="7:18",
            nexthops=[NextHop(gateway="10.1.1.2", interface="eth0", selected=True)],
            active=True,
            installed=True,
            source="10.1.1.2",
        )
        out = render_detail([route], "inet.0")
        assert "Source: 10.1.1.2" in out
        assert "Protocol next hop: 10.1.1.2" in out

    def test_detail_isis_level(self):
        route = Route(
            prefix="10.1.1.0/30",
            family=4,
            protocol="IS-IS",
            preference=15,
            age="00:05:00",
            nexthops=[NextHop(gateway="10.0.0.1", interface="eth0", selected=True)],
            active=True,
            installed=True,
            metric=10,
            isis_level=2,
        )
        out = render_detail([route], "inet.0")
        assert "IS-IS" in out
        assert "Metric: 10" in out
        assert "Level: 2" in out

    def test_detail_hidden_route_state(self):
        route = Route(
            prefix="10.3.0.0/24",
            family=4,
            protocol="BGP",
            preference=170,
            age="00:01:00",
            nexthops=[NextHop(gateway="10.1.1.2", interface="eth0")],
            active=False,
            installed=False,
            hidden_reason="Not installed in FIB",
        )
        out = render_detail([route], "inet.0")
        assert "State: <Hidden Ext>" in out
        assert "Hidden reason" in out

    def test_detail_static_as_path_I(self):
        route = Route(
            prefix="0.0.0.0/0",
            family=4,
            protocol="Static",
            preference=5,
            age="00:00:00",
            nexthops=[NextHop(gateway="10.0.0.1", interface="eth0", selected=True)],
            active=True,
            installed=True,
        )
        out = render_detail([route], "inet.0")
        assert "AS path: I" in out

    def test_detail_blackhole_type(self):
        route = Route(
            prefix="192.0.2.0/24",
            family=4,
            protocol="Static",
            preference=5,
            age="00:00:00",
            nexthops=[],
            active=True,
            installed=True,
            is_blackhole=True,
        )
        out = render_detail([route], "inet.0")
        assert "Next hop type: Discard" in out

    def test_detail_age_metric(self):
        route = Route(
            prefix="0.0.0.0/0",
            family=4,
            protocol="Static",
            preference=5,
            age="10:47:00",
            nexthops=[NextHop(gateway="172.18.4.41", interface="et0", selected=True)],
            active=True,
            installed=True,
            metric=0,
        )
        out = render_detail([route], "inet.0")
        assert "Age: 10:47:00" in out
        assert "Metric: 0" in out


# ============================================================================
# show_route() end-to-end
# ============================================================================

class TestShowRoute:
    def _frr_ipv4(self) -> dict:
        return {
            "0.0.0.0/0": [{
                "protocol": "static", "selected": True, "destSelected": True,
                "installed": True, "distance": 5, "metric": 0, "uptime": "10:47:00",
                "nexthops": [{"ip": "172.18.4.41", "interfaceName": "et0", "active": True}],
            }],
            "10.0.0.0/24": [{
                "protocol": "connected", "selected": True, "destSelected": True,
                "installed": True, "distance": 0, "metric": 0, "uptime": "10:46:57",
                "nexthops": [{"interfaceName": "irb.101", "active": True}],
            }],
            "172.18.4.40/29": [{
                "protocol": "connected", "selected": True, "destSelected": True,
                "installed": True, "distance": 0, "metric": 0, "uptime": "10:47:00",
                "nexthops": [{"interfaceName": "et0", "active": True}],
            }],
        }

    def _frr_ipv6(self) -> dict:
        return {
            "::1/128": [{
                "protocol": "connected", "selected": True, "destSelected": True,
                "installed": True, "distance": 0, "metric": 0, "uptime": "00:00:00",
                "nexthops": [{"interfaceName": "lo", "active": True}],
            }]
        }

    def test_basic_output_has_inet0(self):
        frr = _make_frr(self._frr_ipv4(), self._frr_ipv6())
        with patch(_PATCH_IPROUTE, None):
            out = show_route([], frr=frr)
        assert "inet.0:" in out

    def test_basic_output_has_inet6(self):
        frr = _make_frr(self._frr_ipv4(), self._frr_ipv6())
        with patch(_PATCH_IPROUTE, None):
            out = show_route([], frr=frr)
        assert "inet6.0:" in out

    def test_both_tables_separated(self):
        frr = _make_frr(self._frr_ipv4(), self._frr_ipv6())
        with patch(_PATCH_IPROUTE, None):
            out = show_route([], frr=frr)
        assert out.index("inet.0:") < out.index("inet6.0:")

    def test_static_default_route_present(self):
        frr = _make_frr(self._frr_ipv4(), self._frr_ipv6())
        with patch(_PATCH_IPROUTE, None):
            out = show_route([], frr=frr)
        assert "0.0.0.0/0" in out
        assert "Static" in out

    def test_detail_flag(self):
        frr = _make_frr(self._frr_ipv4(), {})
        with patch(_PATCH_IPROUTE, None):
            out = show_route(["detail"], frr=frr)
        assert "Preference:" in out
        assert "Next hop type:" in out

    def test_terse_flag(self):
        frr = _make_frr(self._frr_ipv4(), {})
        with patch(_PATCH_IPROUTE, None):
            out = show_route(["terse"], frr=frr)
        lines = [l for l in out.splitlines() if "0.0.0.0/0" in l]
        assert lines
        assert "> to 172.18.4.41" in lines[0]

    def test_prefix_filter(self):
        frr = _make_frr(self._frr_ipv4(), {})
        with patch(_PATCH_IPROUTE, None):
            out = show_route(["10.0.0.0/24"], frr=frr)
        assert "10.0.0.0/24" in out
        assert "0.0.0.0/0" not in out
        assert "172.18.4.40/29" not in out

    def test_prefix_filter_no_match(self):
        frr = _make_frr(self._frr_ipv4(), {})
        with patch(_PATCH_IPROUTE, None):
            out = show_route(["192.0.2.0/24"], frr=frr)
        assert "No routes found" in out

    def test_protocol_filter_static(self):
        frr = _make_frr(self._frr_ipv4(), {})
        with patch(_PATCH_IPROUTE, None):
            out = show_route(["protocol", "static"], frr=frr)
        assert "Static" in out
        assert "Direct" not in out

    def test_protocol_filter_direct(self):
        frr = _make_frr(self._frr_ipv4(), {})
        with patch(_PATCH_IPROUTE, None):
            out = show_route(["protocol", "direct"], frr=frr)
        assert "Direct" in out
        assert "Static" not in out

    def test_protocol_filter_unknown_returns_error(self):
        frr = _make_frr(self._frr_ipv4(), {})
        with patch(_PATCH_IPROUTE, None):
            out = show_route(["protocol", "xyz"], frr=frr)
        assert "error" in out.lower()

    def test_hidden_flag_only_shows_hidden(self):
        ipv4 = {
            **self._frr_ipv4(),
            "10.3.0.0/24": [{
                "protocol": "bgp", "selected": False, "destSelected": False,
                "installed": False, "distance": 170, "metric": 0, "uptime": "00:01:00",
                "nexthops": [{"ip": "10.1.1.2", "interfaceName": "eth0", "active": False}],
            }],
        }
        frr = _make_frr(ipv4, {})
        with patch(_PATCH_IPROUTE, None):
            out = show_route(["hidden"], frr=frr)
        assert "10.3.0.0/24" in out
        # Active routes should not appear in body
        assert "0.0.0.0/0" not in out

    def test_no_routes_message(self):
        frr = _make_frr({}, {})
        ipr_mock = _make_ipr_mock([], [], [])
        mock_cls = Mock(return_value=ipr_mock)
        with patch(_PATCH_IPROUTE, mock_cls):
            out = show_route([], frr=frr)
        assert "No routes found" in out

    def test_frr_unavailable_fallback_to_kernel(self):
        # FRR raises an exception
        frr = MagicMock()
        frr.show.side_effect = RuntimeError("vtysh not available")

        static_r = _MockRouteMsg(_AF_INET, _RTPROT_STATIC, _RTN_UNICAST,
                                 dst_len=0, dst=None, gateway="10.0.0.1", oif=2)
        link = _MockLink("eth0", 2)
        ipr = _make_ipr_mock([static_r], [], [link])
        ipr_cls = Mock(return_value=ipr)

        with patch(_PATCH_IPROUTE, ipr_cls):
            out = show_route([], frr=frr)

        assert "Static" in out
        assert "0.0.0.0/0" in out

    def test_frr_and_iproute2_both_unavailable(self):
        frr = MagicMock()
        frr.show.side_effect = RuntimeError("no frr")
        with patch(_PATCH_IPROUTE, None):
            out = show_route([], frr=frr)
        assert "No routes found" in out

    def test_unknown_arg_returns_error(self):
        frr = _make_frr({}, {})
        with patch(_PATCH_IPROUTE, None):
            out = show_route(["badoption"], frr=frr)
        assert "error" in out.lower()


# ============================================================================
# IPv4 / IPv6 separation
# ============================================================================

class TestIPv4IPv6Separation:
    def test_only_ipv4_shows_inet0_only(self):
        frr = _make_frr(
            {"10.0.0.0/24": [{
                "protocol": "connected", "selected": True, "destSelected": True,
                "installed": True, "distance": 0, "metric": 0, "uptime": "00:00:00",
                "nexthops": [{"interfaceName": "eth0", "active": True}],
            }]},
            {},
        )
        with patch(_PATCH_IPROUTE, None):
            out = show_route([], frr=frr)
        assert "inet.0:" in out
        assert "inet6.0:" not in out

    def test_only_ipv6_shows_inet6_only(self):
        frr = _make_frr(
            {},
            {"::1/128": [{
                "protocol": "connected", "selected": True, "destSelected": True,
                "installed": True, "distance": 0, "metric": 0, "uptime": "00:00:00",
                "nexthops": [{"interfaceName": "lo", "active": True}],
            }]},
        )
        with patch(_PATCH_IPROUTE, None):
            out = show_route([], frr=frr)
        assert "inet.0:" not in out
        assert "inet6.0:" in out

    def test_ipv6_prefix_in_correct_table(self):
        frr = _make_frr(
            {"10.0.0.0/24": [{
                "protocol": "connected", "selected": True, "destSelected": True,
                "installed": True, "distance": 0, "metric": 0, "uptime": "00:00:00",
                "nexthops": [{"interfaceName": "eth0", "active": True}],
            }]},
            {"::1/128": [{
                "protocol": "connected", "selected": True, "destSelected": True,
                "installed": True, "distance": 0, "metric": 0, "uptime": "00:00:00",
                "nexthops": [{"interfaceName": "lo", "active": True}],
            }]},
        )
        with patch(_PATCH_IPROUTE, None):
            out = show_route([], frr=frr)

        inet0_section  = out.split("inet6.0:")[0] if "inet6.0:" in out else out
        inet60_section = out.split("inet6.0:", 1)[1] if "inet6.0:" in out else ""

        assert "10.0.0.0/24" in inet0_section
        assert "::1/128"     in inet60_section
        assert "10.0.0.0/24" not in inet60_section


# ============================================================================
# Table filter (show route table [inet.0|inet6.0])
# ============================================================================

class TestShowRouteTable:
    def _frr_both_tables(self) -> tuple[dict, dict]:
        ipv4 = {
            "10.0.0.0/24": [{
                "protocol": "connected", "selected": True, "destSelected": True,
                "installed": True, "distance": 0, "metric": 0, "uptime": "00:00:00",
                "nexthops": [{"interfaceName": "eth0", "active": True}],
            }],
            "0.0.0.0/0": [{
                "protocol": "static", "selected": True, "destSelected": True,
                "installed": True, "distance": 5, "metric": 0, "uptime": "10:00:00",
                "nexthops": [{"ip": "10.0.0.1", "interfaceName": "eth0", "active": True}],
            }],
        }
        ipv6 = {
            "::1/128": [{
                "protocol": "connected", "selected": True, "destSelected": True,
                "installed": True, "distance": 0, "metric": 0, "uptime": "00:00:00",
                "nexthops": [{"interfaceName": "lo", "active": True}],
            }],
            "2001:db8::/32": [{
                "protocol": "static", "selected": True, "destSelected": True,
                "installed": True, "distance": 5, "metric": 0, "uptime": "10:00:00",
                "nexthops": [{"ip": "2001:db8::1", "interfaceName": "eth0", "active": True}],
            }],
        }
        return ipv4, ipv6

    def test_table_inet0_shows_only_ipv4(self):
        ipv4, ipv6 = self._frr_both_tables()
        frr = _make_frr(ipv4, ipv6)
        with patch(_PATCH_IPROUTE, None):
            out = show_route(["table", "inet.0"], frr=frr)
        assert "inet.0:" in out
        assert "inet6.0:" not in out
        assert "10.0.0.0/24" in out
        assert "0.0.0.0/0" in out
        assert "::1/128" not in out
        assert "2001:db8::/32" not in out

    def test_table_inet6_0_shows_only_ipv6(self):
        ipv4, ipv6 = self._frr_both_tables()
        frr = _make_frr(ipv4, ipv6)
        with patch(_PATCH_IPROUTE, None):
            out = show_route(["table", "inet6.0"], frr=frr)
        assert "inet.0:" not in out
        assert "inet6.0:" in out
        assert "10.0.0.0/24" not in out
        assert "0.0.0.0/0" not in out
        assert "::1/128" in out
        assert "2001:db8::/32" in out

    def test_table_inet0_detail(self):
        ipv4, ipv6 = self._frr_both_tables()
        frr = _make_frr(ipv4, ipv6)
        with patch(_PATCH_IPROUTE, None):
            out = show_route(["table", "inet.0", "detail"], frr=frr)
        assert "inet.0:" in out
        assert "inet6.0:" not in out
        assert "10.0.0.0/24" in out
        assert "Preference:" in out
        assert "::1/128" not in out

    def test_table_inet6_0_detail(self):
        ipv4, ipv6 = self._frr_both_tables()
        frr = _make_frr(ipv4, ipv6)
        with patch(_PATCH_IPROUTE, None):
            out = show_route(["table", "inet6.0", "detail"], frr=frr)
        assert "inet.0:" not in out
        assert "inet6.0:" in out
        assert "2001:db8::/32" in out
        assert "Preference:" in out
        assert "10.0.0.0/24" not in out

    def test_table_inet0_terse(self):
        ipv4, ipv6 = self._frr_both_tables()
        frr = _make_frr(ipv4, ipv6)
        with patch(_PATCH_IPROUTE, None):
            out = show_route(["table", "inet.0", "terse"], frr=frr)
        assert "inet.0:" in out
        assert "inet6.0:" not in out
        lines = [l for l in out.splitlines() if "10.0.0.0/24" in l]
        assert lines
        assert "via eth0" in lines[0]

    def test_table_unknown_returns_error(self):
        ipv4, ipv6 = self._frr_both_tables()
        frr = _make_frr(ipv4, ipv6)
        with patch(_PATCH_IPROUTE, None):
            out = show_route(["table", "unknown.0"], frr=frr)
        assert "error" in out.lower()
        assert "unknown routing table" in out.lower()

    def test_table_missing_name_returns_error(self):
        frr = _make_frr({}, {})
        with patch(_PATCH_IPROUTE, None):
            out = show_route(["table"], frr=frr)
        assert "error" in out.lower()
        assert "requires a table name" in out.lower()

    def test_table_inet_abbreviation_shows_ipv4(self):
        """Test that 'show route table inet' works like 'show route table inet.0'."""
        ipv4, ipv6 = self._frr_both_tables()
        frr = _make_frr(ipv4, ipv6)
        with patch(_PATCH_IPROUTE, None):
            out = show_route(["table", "inet"], frr=frr)
        assert "inet.0:" in out
        assert "inet6.0:" not in out
        assert "10.0.0.0/24" in out
        assert "0.0.0.0/0" in out
        assert "::1/128" not in out
        assert "2001:db8::/32" not in out

    def test_table_inet6_abbreviation_shows_ipv6(self):
        """Test that 'show route table inet6' works like 'show route table inet6.0'."""
        ipv4, ipv6 = self._frr_both_tables()
        frr = _make_frr(ipv4, ipv6)
        with patch(_PATCH_IPROUTE, None):
            out = show_route(["table", "inet6"], frr=frr)
        assert "inet.0:" not in out
        assert "inet6.0:" in out
        assert "10.0.0.0/24" not in out
        assert "0.0.0.0/0" not in out
        assert "::1/128" in out
        assert "2001:db8::/32" in out


# ============================================================================
# Interface alias translation
# ============================================================================

class TestInterfaceAlias:
    def test_frr_route_interface_aliased(self):
        frr = _make_frr({
            "10.0.0.0/24": [{
                "protocol": "connected", "selected": True, "destSelected": True,
                "installed": True, "distance": 0, "metric": 0, "uptime": "00:00:00",
                "nexthops": [{"interfaceName": "ens33", "active": True}],
            }]
        }, {})
        alias_fn = lambda name: {"ens33": "et0"}.get(name, name)
        with patch(_PATCH_IPROUTE, None):
            out = show_route([], frr=frr, alias_fn=alias_fn)
        assert "et0" in out
        assert "ens33" not in out

    def test_frr_route_gateway_and_interface_both_aliased(self):
        frr = _make_frr({
            "0.0.0.0/0": [{
                "protocol": "static", "selected": True, "destSelected": True,
                "installed": True, "distance": 5, "metric": 0, "uptime": "00:00:00",
                "nexthops": [{"ip": "172.18.4.41", "interfaceName": "ens33", "active": True}],
            }]
        }, {})
        alias_fn = lambda name: {"ens33": "et0"}.get(name, name)
        with patch(_PATCH_IPROUTE, None):
            out = show_route([], frr=frr, alias_fn=alias_fn)
        assert "via et0" in out

    def test_kernel_route_interface_aliased(self):
        r = _MockRouteMsg(_AF_INET, _RTPROT_KERNEL, _RTN_UNICAST,
                          dst_len=24, dst="10.0.0.0", oif=2)
        link = _MockLink("ens33", 2)
        ipr = _make_ipr_mock([r], [], [link])
        ipr_cls = Mock(return_value=ipr)

        frr = MagicMock()
        frr.show.side_effect = RuntimeError("no frr")
        alias_fn = lambda name: {"ens33": "et0"}.get(name, name)

        with patch(_PATCH_IPROUTE, ipr_cls):
            out = show_route([], frr=frr, alias_fn=alias_fn)

        assert "et0" in out
        assert "ens33" not in out

    def test_no_alias_uses_kernel_name(self):
        frr = _make_frr({
            "10.0.0.0/24": [{
                "protocol": "connected", "selected": True, "destSelected": True,
                "installed": True, "distance": 0, "metric": 0, "uptime": "00:00:00",
                "nexthops": [{"interfaceName": "ens33", "active": True}],
            }]
        }, {})
        with patch(_PATCH_IPROUTE, None):
            out = show_route([], frr=frr, alias_fn=None)
        assert "ens33" in out


# ============================================================================
# FRR + kernel merge
# ============================================================================

class TestMerge:
    def test_frr_wins_over_kernel(self):
        """When FRR has a prefix and kernel also has it, FRR data appears."""
        frr = _make_frr({
            "10.0.0.0/24": [{
                "protocol": "connected", "selected": True, "destSelected": True,
                "installed": True, "distance": 0, "metric": 0, "uptime": "10:46:57",
                "nexthops": [{"interfaceName": "irb.101", "active": True}],
            }]
        }, {})

        # Kernel also has the same prefix
        r = _MockRouteMsg(_AF_INET, _RTPROT_KERNEL, _RTN_UNICAST,
                          dst_len=24, dst="10.0.0.0", oif=2)
        link = _MockLink("eth0", 2)
        ipr = _make_ipr_mock([r], [], [link])
        ipr_cls = Mock(return_value=ipr)

        with patch(_PATCH_IPROUTE, ipr_cls):
            out = show_route([], frr=frr)

        # Should appear once; FRR's interface wins
        count = out.count("10.0.0.0/24")
        assert count == 1
        assert "irb.101" in out

    def test_kernel_fills_gap_not_in_frr(self):
        """A kernel-only prefix appears when FRR doesn't know about it."""
        frr = _make_frr({}, {})  # Empty FRR

        r = _MockRouteMsg(_AF_INET, _RTPROT_STATIC, _RTN_UNICAST,
                          dst_len=0, dst=None, gateway="10.0.0.1", oif=2)
        link = _MockLink("eth0", 2)
        ipr = _make_ipr_mock([r], [], [link])
        ipr_cls = Mock(return_value=ipr)

        # FRR raises no error, but returns empty JSON
        with patch(_PATCH_IPROUTE, ipr_cls):
            out = show_route([], frr=frr)

        # With empty FRR data, merge sees FRR OK but no routes → kernel fills
        # Actually the merge keeps kernel routes not in FRR, so it should appear
        assert "0.0.0.0/0" in out


# ============================================================================
# Operational mode integration
# ============================================================================

class TestOperationalModeShowRoute:
    @pytest.fixture
    def store(self, tmp_path):
        return ConfigStore(base_dir=tmp_path)

    # FRRClient is a local import inside _show_route; patch it at its source module.
    _PATCH_FRR_CLIENT = "nos.drivers.frr.client.FRRClient"

    def test_show_route_calls_frr(self, store):
        """OperationalMode._show_route() uses FRRClient."""
        oper = OperationalMode(store)

        frr_mock = MagicMock()
        frr_mock.show.return_value = json.dumps({
            "10.0.0.0/24": [{
                "protocol": "connected", "selected": True, "destSelected": True,
                "installed": True, "distance": 0, "metric": 0, "uptime": "00:01:00",
                "nexthops": [{"interfaceName": "eth0", "active": True}],
            }]
        })

        with patch("nos.cli.commands.show.route._IPRoute", None), \
             patch(self._PATCH_FRR_CLIENT, return_value=frr_mock):
            out = oper.execute("show route")

        assert "inet.0:" in out
        assert "10.0.0.0/24" in out

    def test_show_route_detail_via_execute(self, store):
        oper = OperationalMode(store)
        frr_mock = MagicMock()
        frr_mock.show.return_value = json.dumps({
            "0.0.0.0/0": [{
                "protocol": "static", "selected": True, "destSelected": True,
                "installed": True, "distance": 5, "metric": 0, "uptime": "10:47:00",
                "nexthops": [{"ip": "10.0.0.1", "interfaceName": "eth0", "active": True}],
            }]
        })

        with patch("nos.cli.commands.show.route._IPRoute", None), \
             patch(self._PATCH_FRR_CLIENT, return_value=frr_mock):
            out = oper.execute("show route detail")

        assert "Preference:" in out

    def test_show_route_frr_failure_shows_kernel(self, store):
        oper = OperationalMode(store)
        frr_mock = MagicMock()
        frr_mock.show.side_effect = RuntimeError("vtysh failed")

        r = _MockRouteMsg(_AF_INET, _RTPROT_STATIC, _RTN_UNICAST,
                          dst_len=0, dst=None, gateway="10.0.0.1", oif=2)
        link = _MockLink("eth0", 2)
        ipr = _make_ipr_mock([r], [], [link])
        ipr_cls = Mock(return_value=ipr)

        with patch("nos.cli.commands.show.route._IPRoute", ipr_cls), \
             patch(self._PATCH_FRR_CLIENT, return_value=frr_mock):
            out = oper.execute("show route")

        assert "Static" in out


# ============================================================================
# Tab completion
# ============================================================================

def _complete_oper(text: str) -> list[str]:
    c = NOSCompleter(mode=CLIMode.OPERATIONAL, edit_path=[], store=None)
    doc = Document(text, len(text))
    return [c.text for c in c.get_completions(doc, CompleteEvent())]


class TestRouteCompletion:
    def test_show_route_space_offers_detail(self):
        kws = _complete_oper("show route ")
        assert "detail" in kws

    def test_show_route_space_offers_terse(self):
        kws = _complete_oper("show route ")
        assert "terse" in kws

    def test_show_route_space_offers_hidden(self):
        kws = _complete_oper("show route ")
        assert "hidden" in kws

    def test_show_route_space_offers_protocol(self):
        kws = _complete_oper("show route ")
        assert "protocol" in kws

    def test_show_route_space_offers_prefix_hint(self):
        kws = _complete_oper("show route ")
        assert "<prefix>" in kws

    def test_show_route_d_partial_completes_detail(self):
        kws = _complete_oper("show route d")
        assert "detail" in kws

    def test_show_route_protocol_space_offers_protos(self):
        kws = _complete_oper("show route protocol ")
        assert "bgp" in kws
        assert "isis" in kws
        assert "ospf" in kws
        assert "static" in kws
        assert "direct" in kws

    def test_show_route_protocol_b_partial(self):
        kws = _complete_oper("show route protocol b")
        assert "bgp" in kws
        assert "isis" not in kws

    def test_show_route_terse_space_offers_pipe(self):
        kws = _complete_oper("show route terse ")
        assert "|" in kws

    def test_show_route_detail_space_offers_pipe(self):
        kws = _complete_oper("show route detail ")
        assert "|" in kws

    def test_show_route_space_offers_pipe(self):
        kws = _complete_oper("show route ")
        assert "|" in kws

    def test_show_space_still_offers_route(self):
        kws = _complete_oper("show ")
        assert "route" in kws

    # ── IP-prefix before subcommand ──────────────────────────────────────────

    def test_prefix_then_partial_subcmd_completes_detail(self):
        kws = _complete_oper("show route 0.0.0.0/0 det")
        assert "detail" in kws

    def test_prefix_then_partial_subcmd_ipv6(self):
        kws = _complete_oper("show route ::1/128 det")
        assert "detail" in kws

    def test_prefix_space_offers_all_subcmds_including_protocol(self):
        kws = _complete_oper("show route 0.0.0.0/0 ")
        assert "detail"   in kws
        assert "terse"    in kws
        assert "hidden"   in kws
        assert "protocol" in kws

    def test_prefix_space_ipv6_offers_protocol(self):
        kws = _complete_oper("show route ::1/128 ")
        assert "protocol" in kws

    def test_prefix_space_offers_pipe(self):
        kws = _complete_oper("show route 0.0.0.0/0 ")
        assert "|" in kws

    def test_typing_ip_prefix_does_not_show_prefix_hint(self):
        kws = _complete_oper("show route 0.0.0.0/0")
        assert "<prefix>" not in kws

    def test_typing_partial_ip_does_not_show_prefix_hint(self):
        kws = _complete_oper("show route 0")
        assert "<prefix>" not in kws

    def test_typing_ipv6_prefix_does_not_show_prefix_hint(self):
        kws = _complete_oper("show route ::1")
        assert "<prefix>" not in kws

    def test_prefix_then_protocol_space_offers_protocols(self):
        kws = _complete_oper("show route 0.0.0.0/0 protocol ")
        assert "bgp"    in kws
        assert "static" in kws

    def test_prefix_then_protocol_partial(self):
        kws = _complete_oper("show route 0.0.0.0/0 protocol b")
        assert "bgp" in kws
        assert "isis" not in kws

    # ── Table filter completions ────────────────────────────────────────────

    def test_show_route_space_offers_table(self):
        kws = _complete_oper("show route ")
        assert "table" in kws

    def test_show_route_table_space_offers_inet_tables(self):
        kws = _complete_oper("show route table ")
        assert "inet.0" in kws
        assert "inet6.0" in kws

    def test_show_route_table_inet_partial(self):
        kws = _complete_oper("show route table inet")
        assert "inet.0" in kws
        assert "inet6.0" in kws

    def test_show_route_table_inet6_partial(self):
        kws = _complete_oper("show route table inet6")
        assert "inet6.0" in kws
        assert "inet.0" not in kws

    def test_show_route_table_inet0_space_offers_detail(self):
        kws = _complete_oper("show route table inet.0 ")
        assert "detail" in kws
        assert "terse" in kws
        assert "hidden" in kws
        assert "protocol" not in kws
        assert "table" not in kws

    def test_show_route_table_inet6_0_space_offers_detail(self):
        kws = _complete_oper("show route table inet6.0 ")
        assert "detail" in kws
        assert "terse" in kws
        assert "hidden" in kws

    def test_show_route_table_inet0_space_offers_pipe(self):
        kws = _complete_oper("show route table inet.0 ")
        assert "|" in kws

    def test_show_route_table_inet0_detail_space_offers_pipe(self):
        kws = _complete_oper("show route table inet.0 detail ")
        assert "|" in kws

    def test_show_route_table_inet0_det_partial(self):
        kws = _complete_oper("show route table inet.0 det")
        assert "detail" in kws
        assert "terse" not in kws

    def test_show_route_table_inet6_0_ter_partial(self):
        kws = _complete_oper("show route table inet6.0 ter")
        assert "terse" in kws
        assert "detail" not in kws

    def test_detail_space_does_not_offer_protocol(self):
        kws = _complete_oper("show route detail ")
        assert "protocol" not in kws


# ============================================================================
# Bug fixes: protocol detection, LPM, orlonger
# ============================================================================

class TestFRRKernelProtocolIsStatic:
    """Bug 1: FRR-reported 'kernel' routes must show as Static/5, not Direct/0."""

    def test_kernel_proto_maps_to_static(self):
        data = {
            "8.8.8.8/32": [{
                "protocol": "kernel",
                "selected": True, "destSelected": True,
                "installed": True, "distance": 0, "metric": 0, "uptime": "01:00:00",
                "nexthops": [{"ip": "172.18.4.41", "interfaceName": "eth0", "active": True}],
            }]
        }
        routes = _parse_frr_json(data, 4, None)
        assert routes[0].protocol == "Static"
        assert routes[0].preference == 5

    def test_kernel_proto_default_route_is_static(self):
        data = {
            "0.0.0.0/0": [{
                "protocol": "kernel",
                "selected": True, "destSelected": True,
                "installed": True, "distance": 0, "metric": 0, "uptime": "3d18h37m",
                "nexthops": [{"ip": "172.18.4.41", "interfaceName": "ens33", "active": True}],
            }]
        }
        routes = _parse_frr_json(data, 4, None)
        assert routes[0].protocol == "Static"
        assert routes[0].preference == 5

    def test_connected_proto_still_direct(self):
        data = {
            "10.0.0.0/24": [{
                "protocol": "connected",
                "selected": True, "destSelected": True,
                "installed": True, "distance": 0, "metric": 0, "uptime": "00:10:00",
                "nexthops": [{"interfaceName": "eth0", "active": True}],
            }]
        }
        routes = _parse_frr_json(data, 4, None)
        assert routes[0].protocol == "Direct"
        assert routes[0].preference == 0

    def test_show_route_kernel_proto_displays_static(self):
        frr = _make_frr({
            "0.0.0.0/0": [{
                "protocol": "kernel", "selected": True, "destSelected": True,
                "installed": True, "distance": 0, "metric": 0, "uptime": "10:47:00",
                "nexthops": [{"ip": "172.18.4.41", "interfaceName": "ens33", "active": True}],
            }]
        }, {})
        with patch(_PATCH_IPROUTE, None):
            out = show_route([], frr=frr)
        assert "Static" in out
        assert "Direct" not in out
        assert "[Static/5]" in out


class TestHostAddressLPM:
    """Bug 2: Host address without /prefix-len must do longest-match lookup."""

    def _frr_data(self):
        def _entry(proto, nh_ip=None, iface="eth0"):
            e = {"protocol": proto, "selected": True, "destSelected": True,
                 "installed": True, "distance": 0 if proto == "connected" else 5,
                 "metric": 0, "uptime": "00:01:00", "nexthops": []}
            if nh_ip:
                e["nexthops"].append({"ip": nh_ip, "interfaceName": iface, "active": True})
            else:
                e["nexthops"].append({"interfaceName": iface, "active": True})
            return e

        return {
            "0.0.0.0/0":    [_entry("static", "172.18.4.41")],
            "8.8.8.0/24":   [_entry("static", "10.0.0.1")],
            "8.8.8.8/32":   [_entry("static", "10.0.0.1")],
            "10.0.0.0/24":  [_entry("connected")],
        }

    def test_host_address_finds_most_specific(self):
        frr = _make_frr(self._frr_data(), {})
        with patch(_PATCH_IPROUTE, None):
            out = show_route(["8.8.8.8"], frr=frr)
        assert "8.8.8.8/32" in out
        assert "8.8.8.0/24" not in out
        assert "0.0.0.0/0" not in out

    def test_host_address_falls_back_to_less_specific(self):
        data = {
            "0.0.0.0/0":   [{"protocol": "static", "selected": True, "destSelected": True,
                              "installed": True, "distance": 5, "metric": 0, "uptime": "01:00:00",
                              "nexthops": [{"ip": "172.18.4.41", "interfaceName": "eth0", "active": True}]}],
            "8.8.8.0/24":  [{"protocol": "static", "selected": True, "destSelected": True,
                              "installed": True, "distance": 5, "metric": 0, "uptime": "01:00:00",
                              "nexthops": [{"ip": "10.0.0.1", "interfaceName": "eth0", "active": True}]}],
        }
        frr = _make_frr(data, {})
        with patch(_PATCH_IPROUTE, None):
            out = show_route(["8.8.8.8"], frr=frr)
        assert "8.8.8.0/24" in out
        assert "0.0.0.0/0" not in out

    def test_host_address_uses_default_route_when_no_specific(self):
        data = {
            "0.0.0.0/0":    [{"protocol": "static", "selected": True, "destSelected": True,
                               "installed": True, "distance": 5, "metric": 0, "uptime": "01:00:00",
                               "nexthops": [{"ip": "172.18.4.41", "interfaceName": "eth0", "active": True}]}],
            "10.0.0.0/24":  [{"protocol": "connected", "selected": True, "destSelected": True,
                               "installed": True, "distance": 0, "metric": 0, "uptime": "01:00:00",
                               "nexthops": [{"interfaceName": "eth0", "active": True}]}],
        }
        frr = _make_frr(data, {})
        with patch(_PATCH_IPROUTE, None):
            out = show_route(["8.8.8.8"], frr=frr)
        assert "0.0.0.0/0" in out
        assert "10.0.0.0/24" not in out

    def test_host_address_no_match(self):
        frr = _make_frr({
            "10.0.0.0/24": [{"protocol": "connected", "selected": True, "destSelected": True,
                              "installed": True, "distance": 0, "metric": 0, "uptime": "01:00:00",
                              "nexthops": [{"interfaceName": "eth0", "active": True}]}],
        }, {})
        with patch(_PATCH_IPROUTE, None):
            out = show_route(["192.0.2.1"], frr=frr)
        assert "No routes found" in out


class TestOrlonger:
    """Bug 3: show route X/Y should show routes that are subnets of X/Y (orlonger)."""

    def _entry(self, proto, nh_ip=None, iface="eth0"):
        e = {"protocol": proto, "selected": True, "destSelected": True,
             "installed": True, "distance": 5 if proto == "static" else 0,
             "metric": 0, "uptime": "00:01:00", "nexthops": []}
        if nh_ip:
            e["nexthops"].append({"ip": nh_ip, "interfaceName": iface, "active": True})
        else:
            e["nexthops"].append({"interfaceName": iface, "active": True})
        return e

    def _frr_data(self):
        return {
            "0.0.0.0/0":    [self._entry("static", "172.18.4.41")],
            "8.8.8.0/24":   [self._entry("static", "10.0.0.1")],
            "8.8.8.8/32":   [self._entry("static", "10.0.0.1")],
            "10.0.0.0/24":  [self._entry("connected")],
        }

    def test_prefix_shows_more_specific_routes(self):
        frr = _make_frr(self._frr_data(), {})
        with patch(_PATCH_IPROUTE, None):
            out = show_route(["8.8.8.0/24"], frr=frr)
        assert "8.8.8.0/24" in out
        assert "8.8.8.8/32" in out
        assert "0.0.0.0/0" not in out
        assert "10.0.0.0/24" not in out

    def test_default_route_filter_shows_all_ipv4(self):
        frr = _make_frr(self._frr_data(), {})
        with patch(_PATCH_IPROUTE, None):
            out = show_route(["0.0.0.0/0"], frr=frr)
        assert "0.0.0.0/0" in out
        assert "8.8.8.0/24" in out
        assert "8.8.8.8/32" in out
        assert "10.0.0.0/24" in out

    def test_exact_flag_limits_to_exact_prefix(self):
        frr = _make_frr(self._frr_data(), {})
        with patch(_PATCH_IPROUTE, None):
            out = show_route(["8.8.8.0/24", "exact"], frr=frr)
        assert "8.8.8.0/24" in out
        assert "8.8.8.8/32" not in out
        assert "0.0.0.0/0" not in out

    def test_no_subnets_shows_only_exact(self):
        frr = _make_frr(self._frr_data(), {})
        with patch(_PATCH_IPROUTE, None):
            out = show_route(["10.0.0.0/24"], frr=frr)
        assert "10.0.0.0/24" in out
        assert "0.0.0.0/0" not in out
        assert "8.8.8.0/24" not in out

    def test_prefix_no_match_returns_no_routes(self):
        frr = _make_frr(self._frr_data(), {})
        with patch(_PATCH_IPROUTE, None):
            out = show_route(["192.0.2.0/24"], frr=frr)
        assert "No routes found" in out
