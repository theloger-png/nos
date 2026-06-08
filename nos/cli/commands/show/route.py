"""JunOS-style 'show route' implementation.

Combines routes from two sources:
  - FRR (vtysh JSON): primary source; all protocol routes
  - Kernel (pyroute2): fallback when FRR is unavailable; also supplements local/direct gaps

Command variants:
  show route
  show route detail
  show route terse
  show route hidden
  show route <prefix>
  show route <prefix> detail
  show route protocol [bgp|isis|ospf|static|direct]
  show route table [inet.0|inet6.0]
  show route table [inet.0|inet6.0] detail
  show route advertising-protocol bgp <neighbor-ip>
  show route receive-protocol bgp <neighbor-ip>
"""
from __future__ import annotations

import ipaddress
import json
import logging
import re
from dataclasses import dataclass, field
from typing import Callable, Optional, TYPE_CHECKING

if TYPE_CHECKING:
    from nos.drivers.frr.client import FRRClient

try:
    from pyroute2 import IPRoute as _IPRoute
except ImportError:  # pragma: no cover
    _IPRoute = None  # type: ignore[assignment,misc]

_LOG = logging.getLogger(__name__)

# ── Linux kernel constants ───────────────────────────────────────────────────

_AF_INET  = 2
_AF_INET6 = 10

_RTN_UNICAST     = 1
_RTN_LOCAL       = 2
_RTN_BROADCAST   = 3
_RTN_BLACKHOLE   = 6
_RTN_UNREACHABLE = 7
_RTN_PROHIBIT    = 8

_RTPROT_KERNEL = 2
_RTPROT_BOOT   = 3
_RTPROT_STATIC = 4

# Kernel protos we read directly (Direct / Static / Local).
# BGP/IS-IS/OSPF come from FRR JSON only.
_KERNEL_PROTO_ACCEPT = frozenset({_RTPROT_KERNEL, _RTPROT_BOOT, _RTPROT_STATIC})

# ── FRR protocol map ─────────────────────────────────────────────────────────

# FRR JSON "protocol" → (JunOS display name, default admin distance)
_FRR_PROTO: dict[str, tuple[str, int]] = {
    "bgp":       ("BGP",    170),
    "isis":      ("IS-IS",  15),
    "ospf":      ("OSPF",   10),
    "ospf6":     ("OSPF",   10),
    "static":    ("Static",  5),
    "connected": ("Direct",  0),
    "kernel":    ("Direct",  0),
    "local":     ("Local",   0),
    "rip":       ("RIP",   100),
    "ripng":     ("RIP",   100),
}

# CLI keyword → JunOS protocol name (for "show route protocol X")
_PROTO_FILTER_MAP: dict[str, str] = {
    "bgp":    "BGP",
    "isis":   "IS-IS",
    "ospf":   "OSPF",
    "static": "Static",
    "direct": "Direct",
    "local":  "Local",
}

# Protocols that get the "Ext" state flag (learned from external protocols)
_EXT_PROTOCOLS = frozenset({"BGP"})

# ── Data model ───────────────────────────────────────────────────────────────

@dataclass
class NextHop:
    gateway: Optional[str] = None  # IP address of next-hop router
    interface: str = ""            # Outgoing interface name
    selected: bool = True          # Is this the active nexthop?


@dataclass
class Route:
    prefix: str
    family: int = 4                # 4 = inet.0, 6 = inet6.0
    protocol: str = "Direct"       # JunOS protocol name
    preference: int = 0            # Admin distance
    age: str = "00:00:00"
    nexthops: list[NextHop] = field(default_factory=list)
    active: bool = True            # Best/selected route for this prefix
    installed: bool = True         # In kernel FIB (False → hidden)
    metric: int = 0
    is_local: bool = False         # RTN_LOCAL — "Local via <iface>"
    is_blackhole: bool = False     # RTN_BLACKHOLE
    is_reject: bool = False        # RTN_UNREACHABLE / RTN_PROHIBIT
    # BGP attributes
    as_path: str = ""
    communities: str = ""
    local_pref: Optional[int] = None
    med: Optional[int] = None
    source: str = ""               # Peer IP
    router_id: str = ""
    local_as: Optional[int] = None
    peer_as: Optional[int] = None
    cluster_list: str = ""
    originator: str = ""
    # IS-IS attributes
    isis_level: Optional[int] = None
    # Hidden route reason string
    hidden_reason: str = ""


# ── Helpers ──────────────────────────────────────────────────────────────────

def _make_prefix(dst: Optional[str], dst_len: int, family: int) -> str:
    if dst is None:
        return "0.0.0.0/0" if family == _AF_INET else "::/0"
    return f"{dst}/{dst_len}"


def _is_linklocal(prefix: str) -> bool:
    """True if the prefix is an IPv6 link-local address/network."""
    return prefix.lower().startswith("fe80")


_PREFIX_RE = re.compile(r'^[\d:.a-fA-F]+(?:/\d+)?$')


def _looks_like_prefix(s: str) -> bool:
    return bool(_PREFIX_RE.match(s))


def _sort_key(prefix: str) -> tuple:
    try:
        net = ipaddress.ip_network(prefix, strict=False)
        return (int(net.network_address), net.prefixlen)
    except Exception:
        return (0, 0)


# ── Kernel route reader ──────────────────────────────────────────────────────

def _read_kernel_routes(
    ipr,
    idx_to_name: dict[int, str],
    alias_fn: Optional[Callable[[str], str]],
) -> list[Route]:
    """Parse the kernel routing table into Route objects."""

    def _iface(oif: Optional[int]) -> str:
        if oif is None:
            return ""
        name = idx_to_name.get(oif, f"if{oif}")
        return alias_fn(name) if alias_fn else name

    routes: list[Route] = []

    for family in (_AF_INET, _AF_INET6):
        try:
            kernel_routes = ipr.get_routes(family=family)
        except Exception as exc:  # pragma: no cover
            _LOG.warning("kernel route read failed (family=%d): %s", family, exc)
            continue

        for r in kernel_routes:
            proto = r["proto"]
            rtype = r["type"]
            dst   = r.get_attr("RTA_DST")
            dlen  = r["dst_len"]
            gw    = r.get_attr("RTA_GATEWAY")
            oif   = r.get_attr("RTA_OIF")

            # Skip broadcast
            if rtype == _RTN_BROADCAST:
                continue

            prefix = _make_prefix(dst, dlen, family)
            fam    = 4 if family == _AF_INET else 6

            # Skip IPv6 link-local
            if fam == 6 and _is_linklocal(prefix):
                continue

            iface = _iface(oif)

            if rtype == _RTN_LOCAL:
                proto_name, pref = "Local", 0
                is_local = True
                is_bh = is_rej = False
            elif rtype == _RTN_BLACKHOLE:
                if proto not in _KERNEL_PROTO_ACCEPT:
                    continue
                proto_name, pref = "Static", 5
                is_local = is_rej = False
                is_bh = True
            elif rtype in (_RTN_UNREACHABLE, _RTN_PROHIBIT):
                if proto not in _KERNEL_PROTO_ACCEPT:
                    continue
                proto_name, pref = "Static", 5
                is_local = is_bh = False
                is_rej = True
            elif proto in _KERNEL_PROTO_ACCEPT:
                proto_name, pref = {
                    _RTPROT_KERNEL: ("Direct", 0),
                    _RTPROT_BOOT:   ("Direct", 0),
                    _RTPROT_STATIC: ("Static", 5),
                }[proto]
                is_local = is_bh = is_rej = False
            else:
                continue  # FRR-installed route; skip (FRR JSON wins)

            routes.append(Route(
                prefix=prefix,
                family=fam,
                protocol=proto_name,
                preference=pref,
                nexthops=[NextHop(gateway=gw, interface=iface, selected=True)],
                active=True,
                installed=True,
                is_local=is_local,
                is_blackhole=is_bh,
                is_reject=is_rej,
            ))

    return routes


# ── FRR JSON parsers ─────────────────────────────────────────────────────────

def _parse_frr_json(
    data: dict,
    family: int,
    alias_fn: Optional[Callable[[str], str]],
) -> list[Route]:
    """Parse FRR 'show ip/ipv6 route json' output into Route objects."""
    routes: list[Route] = []

    for prefix_str, entries in data.items():
        if not isinstance(entries, list):
            continue
        if family == 6 and _is_linklocal(prefix_str):
            continue

        for entry in entries:
            if not isinstance(entry, dict):
                continue

            proto_raw   = entry.get("protocol", "").lower()
            proto_name, default_pref = _FRR_PROTO.get(proto_raw, ("Unknown", 1))
            distance    = entry.get("distance", default_pref)
            metric      = entry.get("metric", 0) or 0
            uptime      = entry.get("uptime", "00:00:00") or "00:00:00"
            selected    = bool(entry.get("selected",     False))
            installed   = bool(entry.get("installed",    False))
            dest_sel    = bool(entry.get("destSelected", selected))

            nexthops: list[NextHop] = []
            for nh_raw in (entry.get("nexthops") or []):
                if not isinstance(nh_raw, dict):
                    continue
                # gateway IP: FRR uses "ip" key, some versions "gateway"
                gw    = nh_raw.get("ip") or nh_raw.get("gateway") or None
                iface = nh_raw.get("interfaceName", "") or ""
                if alias_fn and iface:
                    iface = alias_fn(iface)
                nh_active = bool(nh_raw.get("active", nh_raw.get("fib", False)))
                if gw or iface:
                    nexthops.append(NextHop(
                        gateway=gw,
                        interface=iface,
                        selected=nh_active,
                    ))

            hidden_reason = "" if installed else "Not installed in FIB"

            routes.append(Route(
                prefix=prefix_str,
                family=family,
                protocol=proto_name,
                preference=distance,
                age=uptime,
                nexthops=nexthops,
                active=dest_sel,
                installed=installed,
                metric=metric,
                is_local=(proto_raw == "local"),
                hidden_reason=hidden_reason,
            ))

    return routes


def _enrich_bgp(
    frr: "FRRClient",
    routes: list[Route],
    family: int,
) -> None:
    """Augment BGP routes with full attributes from 'show ip bgp json'."""
    bgp_routes = [r for r in routes if r.protocol == "BGP"]
    if not bgp_routes:
        return

    cmd = "show ip bgp json" if family == 4 else "show ipv6 bgp json"
    try:
        raw = frr.show(cmd)
        data = json.loads(raw)
    except Exception as exc:
        _LOG.debug("%s fetch failed: %s", cmd, exc)
        return

    bgp_table: dict = data.get("routes", {})

    def _is_bestpath(entry):
        bp = entry.get("bestpath", False)
        if isinstance(bp, dict):
            return bp.get("overall", False)
        return bool(bp)

    for route in bgp_routes:
        entries = bgp_table.get(route.prefix, [])
        if not entries:
            continue

        # Prefer the entry with bestpath.overall = True
        best = next(
            (e for e in entries if _is_bestpath(e)),
            entries[0] if entries else None,
        )
        if best is None:
            continue

        aspath = best.get("aspath", {})
        route.as_path = (aspath.get("string", "") or "") if isinstance(aspath, dict) else ""

        community = best.get("community", {})
        route.communities = (community.get("string", "") or "") if isinstance(community, dict) else ""

        route.local_pref = best.get("localpref")
        route.med        = best.get("med")
        route.source     = best.get("peerId", "") or ""
        route.router_id  = best.get("routerId", "") or ""

        cluster = best.get("clusterList")
        if isinstance(cluster, list):
            route.cluster_list = " ".join(str(c) for c in cluster)

        route.originator = best.get("originatorId", "") or ""

        # Origin code → append to as_path if not already there
        origin_code = best.get("originCode", "")
        if origin_code and not route.as_path.endswith(origin_code):
            route.as_path = (route.as_path + " " + origin_code).strip()


# ── BGP adjacency-RIB (advertising / receiving) ──────────────────────────────

_ORIGIN_CODE: dict[str, str] = {"IGP": "I", "EGP": "E", "incomplete": "?"}

_ADJ_TABLE_HDR = (
    "  "
    + f"{'Prefix':<24}"
    + f"{'Nexthop':<15}"
    + f"{'MED':>6}"
    + "  "
    + f"{'Lclpref':>7}"
    + "  AS path"
)


def _frr_adj_fetch(frr: "FRRClient", cmd: str) -> tuple[dict, Optional[str]]:
    """Run *cmd* via FRR; return (parsed_dict, error_str_or_None)."""
    try:
        raw = frr.show(cmd)
    except Exception as exc:
        _LOG.debug("FRR %r failed: %s", cmd, exc)
        return {}, str(exc)
    try:
        return json.loads(raw), None
    except json.JSONDecodeError:
        raw_lower = raw.lower()
        if "no such neighbor" in raw_lower or "unknown neighbor" in raw_lower:
            return {}, "neighbor_not_found"
        _LOG.debug("FRR %r returned non-JSON: %.120s", cmd, raw)
        return {}, raw.strip()


def _parse_bgp_adj_json(data: dict, route_key: str) -> list[dict]:
    """Parse FRR adj-RIB JSON (*advertisedRoutes* or *receivedRoutes*)."""
    routes_raw = data.get(route_key, {})
    if not isinstance(routes_raw, dict):
        return []

    result: list[dict] = []
    for prefix_str, entry in routes_raw.items():
        if not isinstance(entry, dict):
            continue

        nexthop   = entry.get("nextHop") or entry.get("nexthop") or ""
        local_pref = entry.get("locPrf") or entry.get("localPref")
        med_raw   = entry.get("metric")
        med       = med_raw if med_raw else None   # treat 0/None as blank

        path_raw = entry.get("path") or entry.get("aspath") or ""
        if isinstance(path_raw, dict):
            path_raw = path_raw.get("string", "")
        origin = entry.get("origin", "IGP")
        origin_code = _ORIGIN_CODE.get(origin, "I")
        as_path = f"{path_raw} {origin_code}".strip() if path_raw else origin_code

        result.append({
            "prefix":     prefix_str,
            "nexthop":    nexthop,
            "local_pref": local_pref,
            "med":        med,
            "as_path":    as_path,
        })

    return sorted(result, key=lambda r: _sort_key(r["prefix"]))


def _render_adj_table(routes: list[dict], table_name: str) -> str:
    """Render one adj-RIB table in JunOS style."""
    lines: list[str] = [
        f"  {table_name}: {len(routes)} destinations",
        "",
        _ADJ_TABLE_HDR,
    ]
    for r in routes:
        nexthop = r["nexthop"]
        if not nexthop or nexthop in ("0.0.0.0", "::"):
            nexthop = "Self"
        med_s = str(r["med"])        if r["med"]        is not None else ""
        lp_s  = str(r["local_pref"]) if r["local_pref"] is not None else ""
        lines.append(
            f"  {r['prefix']:<24}{nexthop:<15}{med_s:>6}  {lp_s:>7}  {r['as_path']}"
        )
    return "\n".join(lines)


def show_route_adj_protocol(
    args: list[str],
    frr: Optional["FRRClient"],
    advertised: bool,
) -> str:
    """Implement show route advertising-protocol/receive-protocol bgp <ip>."""
    verb = "advertising-protocol" if advertised else "receive-protocol"

    if frr is None:
        return "BGP is not running"

    if len(args) < 2:
        return f"error: usage: show route {verb} bgp <neighbor-ip>"
    if args[0].lower() != "bgp":
        return f"error: expected 'bgp' after '{verb}', got '{args[0]}'"

    neighbor_ip = args[1]
    try:
        ipaddress.ip_address(neighbor_ip)
    except ValueError:
        return f"error: invalid IP address: '{neighbor_ip}'"

    route_key  = "advertisedRoutes" if advertised else "receivedRoutes"
    cmd_suffix = "advertised-routes json" if advertised else "received-routes json"

    data4, err4 = _frr_adj_fetch(frr, f"show ip bgp neighbors {neighbor_ip} {cmd_suffix}")
    data6, err6 = _frr_adj_fetch(frr, f"show bgp ipv6 unicast neighbors {neighbor_ip} {cmd_suffix}")

    if err4 == "neighbor_not_found" and err6 == "neighbor_not_found":
        return f"BGP neighbor {neighbor_ip!r} not found."

    parts: list[str] = []

    routes4 = _parse_bgp_adj_json(data4, route_key)
    if routes4:
        parts.append(_render_adj_table(routes4, "inet.0"))

    routes6 = _parse_bgp_adj_json(data6, route_key)
    if routes6:
        parts.append(_render_adj_table(routes6, "inet6.0"))

    if not parts:
        verb_label = "advertised to" if advertised else "received from"
        return f"No routes {verb_label} {neighbor_ip}."

    return "\n\n".join(parts)


# ── Route table builder ──────────────────────────────────────────────────────

def _merge(frr_routes: list[Route], kernel_routes: list[Route]) -> list[Route]:
    """FRR routes take precedence; kernel fills any gaps not covered by FRR."""
    result: dict[str, Route] = {}
    for r in frr_routes:
        result[r.prefix] = r
    for r in kernel_routes:
        if r.prefix not in result:
            result[r.prefix] = r
    return list(result.values())


def _build_route_table(
    frr: Optional["FRRClient"],
    alias_fn: Optional[Callable[[str], str]],
    detail: bool = False,
) -> tuple[list[Route], list[Route]]:
    """Return (ipv4_routes, ipv6_routes) combining FRR and kernel data."""
    frr4: list[Route] = []
    frr6: list[Route] = []
    frr_ok = False

    if frr is not None:
        try:
            frr4 = _parse_frr_json(json.loads(frr.show("show ip route json")),    4, alias_fn)
            frr_ok = True
        except Exception as exc:
            _LOG.warning("FRR IPv4 route fetch failed: %s", exc)

        try:
            frr6 = _parse_frr_json(json.loads(frr.show("show ipv6 route json")), 6, alias_fn)
            frr_ok = True
        except Exception as exc:
            _LOG.warning("FRR IPv6 route fetch failed: %s", exc)

        if detail:
            _enrich_bgp(frr, frr4, 4)
            _enrich_bgp(frr, frr6, 6)

    # Kernel fallback (or supplement for missing local/direct entries)
    k4: list[Route] = []
    k6: list[Route] = []

    if _IPRoute is not None:
        try:
            with _IPRoute() as ipr:
                links = ipr.get_links()
                idx_to_name: dict[int, str] = {
                    lnk["index"]: lnk.get_attr("IFLA_IFNAME")
                    for lnk in links
                    if lnk.get_attr("IFLA_IFNAME")
                }
                kr = _read_kernel_routes(ipr, idx_to_name, alias_fn)
                k4 = [r for r in kr if r.family == 4]
                k6 = [r for r in kr if r.family == 6]
        except Exception as exc:
            _LOG.warning("kernel route read failed: %s", exc)

    routes4 = _merge(frr4, k4) if frr_ok else k4
    routes6 = _merge(frr6, k6) if frr_ok else k6
    return routes4, routes6


# ── Rendering helpers ────────────────────────────────────────────────────────

def _active_marker(route: Route) -> str:
    """Return '*', '-', or ' ' for the route state indicator."""
    if route.active and route.installed:
        return "*"
    if route.installed and not route.active:
        return "-"
    return " "


def _nh_type(route: Route) -> str:
    if route.is_blackhole:
        return "Discard"
    if route.is_reject:
        return "Reject"
    if route.is_local:
        return "Local"
    if any(nh.gateway for nh in route.nexthops):
        return "Router"
    return "Interface"


def _state_flags(route: Route) -> str:
    flags: list[str] = []
    if not route.installed:
        flags.append("Hidden")
    else:
        if route.active:
            flags.append("Active")
        flags.append("Int")
    if route.protocol in _EXT_PROTOCOLS:
        flags.append("Ext")
    return "<" + " ".join(flags) + ">"


def _nexthop_lines_brief(route: Route, indent: int) -> list[str]:
    """Return indented nexthop line(s) for brief format."""
    pad = " " * indent
    lines: list[str] = []

    if route.is_blackhole:
        lines.append(f"{pad}Discard")
        return lines
    if route.is_reject:
        lines.append(f"{pad}Reject")
        return lines

    active_nhs = [nh for nh in route.nexthops if nh.selected] or route.nexthops
    if not active_nhs:
        active_nhs = [NextHop()]

    for i, nh in enumerate(active_nhs):
        if route.is_local:
            via = f" via {nh.interface}" if nh.interface else ""
            lines.append(f"{pad}  Local{via}")
        elif nh.gateway:
            via = f" via {nh.interface}" if nh.interface else ""
            prefix_ch = "> " if i == 0 else "  "
            lines.append(f"{pad}{prefix_ch}to {nh.gateway}{via}")
        elif nh.interface:
            prefix_ch = "> " if i == 0 else "  "
            lines.append(f"{pad}{prefix_ch}via {nh.interface}")
        else:
            lines.append(f"{pad}>")

    return lines


def _nexthop_text_terse(route: Route) -> str:
    """Single-line nexthop text for terse format (no leading indent)."""
    if route.is_blackhole:
        return "Discard"
    if route.is_reject:
        return "Reject"

    active_nhs = [nh for nh in route.nexthops if nh.selected] or route.nexthops
    if not active_nhs:
        return ""

    nh = active_nhs[0]
    if route.is_local:
        via = f" via {nh.interface}" if nh.interface else ""
        return f"Local{via}"
    if nh.gateway:
        via = f" via {nh.interface}" if nh.interface else ""
        return f"> to {nh.gateway}{via}"
    if nh.interface:
        return f"> via {nh.interface}"
    return ">"


def _prefix_line(route: Route, pw: int) -> str:
    """Prefix + indicator + protocol + age line."""
    marker = _active_marker(route)
    return (
        f"{route.prefix:<{pw}}{marker}"
        f"[{route.protocol}/{route.preference}] "
        f"{route.age}"
    )


def _table_stats(routes: list[Route]) -> tuple[int, int, int, int]:
    """(destinations, total_routes, active, hidden)."""
    dests   = len({r.prefix for r in routes})
    total   = len(routes)
    active  = sum(1 for r in routes if r.active and r.installed)
    hidden  = sum(1 for r in routes if not r.installed)
    return dests, total, active, hidden


def _header(table_name: str, routes: list[Route]) -> str:
    dests, total, active, hidden = _table_stats(routes)
    return (
        f"{table_name}: {dests} destinations, {total} routes"
        f" ({active} active, 0 holddown, {hidden} hidden)"
    )


def _pw(routes: list[Route]) -> int:
    """Compute prefix column width: max prefix length + 2, minimum 20."""
    if not routes:
        return 20
    return max(20, max(len(r.prefix) for r in routes) + 2)


# ── Renderers ────────────────────────────────────────────────────────────────

def render_brief(
    display: list[Route],
    table_name: str,
    all_routes: Optional[list[Route]] = None,
) -> str:
    """Brief format (default 'show route')."""
    ref = all_routes if all_routes is not None else display
    pw  = _pw(display)
    lines: list[str] = [
        _header(table_name, ref),
        "",
        "+ = Active Route, - = Last Active, * = Both",
        "",
    ]
    for route in display:
        lines.append(_prefix_line(route, pw))
        lines.extend(_nexthop_lines_brief(route, pw))
    return "\n".join(lines)


def render_terse(
    display: list[Route],
    table_name: str,
    all_routes: Optional[list[Route]] = None,
) -> str:
    """Terse format: one line per route."""
    ref = all_routes if all_routes is not None else display
    pw  = _pw(display)
    lines: list[str] = [
        _header(table_name, ref),
        "",
        "+ = Active Route, - = Last Active, * = Both",
        "",
    ]
    for route in display:
        prefix_part = _prefix_line(route, pw)
        nh_part     = _nexthop_text_terse(route)
        lines.append(f"{prefix_part} {nh_part}" if nh_part else prefix_part)
    return "\n".join(lines)


def _render_route_detail(route: Route) -> list[str]:
    I8  = "        "   # 8 spaces
    I16 = "                "  # 16 spaces
    lines: list[str] = []

    lines.append(f"{route.prefix} (1 entry, 1 announced)")

    marker = "*" if (route.active and route.installed) else " "
    lines.append(f"{I8}{marker}{route.protocol} Preference: {route.preference}")

    nh_type_str = _nh_type(route)
    lines.append(f"{I16}Next hop type: {nh_type_str}")

    active_nhs = [nh for nh in route.nexthops if nh.selected] or route.nexthops
    nh_count   = max(len(active_nhs), 1)
    lines.append(f"{I16}Next-hop reference count: {nh_count}")

    # BGP peer source before nexthop
    if route.protocol == "BGP" and route.source:
        lines.append(f"{I16}Source: {route.source}")

    # Nexthop detail lines
    if not route.is_blackhole and not route.is_reject and not route.is_local:
        for nh in (active_nhs or [NextHop()]):
            via_part = f" via {nh.interface}" if nh.interface else ""
            sel_part = ", selected" if nh.selected else ""
            if nh.gateway:
                lines.append(f"{I16}Next hop: {nh.gateway}{via_part}{sel_part}")
            else:
                lines.append(f"{I16}Next hop:{via_part}{sel_part}")

        if route.protocol == "BGP":
            nh_ip = next((nh.gateway for nh in active_nhs if nh.gateway), "")
            if nh_ip:
                lines.append(f"{I16}Protocol next hop: {nh_ip}")

    lines.append(f"{I16}State: {_state_flags(route)}")

    if route.protocol == "BGP":
        if route.local_as is not None:
            lines.append(f"{I16}Local AS: {route.local_as}")
        if route.peer_as is not None:
            lines.append(f"{I16}Peer AS: {route.peer_as}")

    lines.append(f"{I16}Age: {route.age}")
    lines.append(f"{I16}Metric: {route.metric}")

    if route.protocol == "BGP":
        if route.med is not None:
            lines.append(f"{I16}Metric2: {route.med}")
        as_path = route.as_path if route.as_path else "I"
        lines.append(f"{I16}AS path: {as_path}")
        if route.communities:
            lines.append(f"{I16}Communities: {route.communities}")
        if route.local_pref is not None:
            lines.append(f"{I16}Localpref: {route.local_pref}")
        if route.cluster_list:
            lines.append(f"{I16}Cluster list: {route.cluster_list}")
        if route.router_id:
            lines.append(f"{I16}Router ID: {route.router_id}")
        if route.originator:
            lines.append(f"{I16}Originator ID: {route.originator}")
        lines.append(f"{I16}Validation State: unverified")

    elif route.protocol == "IS-IS":
        if route.isis_level is not None:
            lines.append(f"{I16}Level: {route.isis_level}")

    elif route.protocol in ("Static",) and not (route.is_blackhole or route.is_reject):
        lines.append(f"{I16}AS path: I")

    if route.hidden_reason:
        lines.append(f"{I16}Hidden reason: {route.hidden_reason}")

    return lines


def render_detail(
    display: list[Route],
    table_name: str,
    all_routes: Optional[list[Route]] = None,
) -> str:
    """Detail format ('show route detail')."""
    ref = all_routes if all_routes is not None else display
    lines: list[str] = [_header(table_name, ref), ""]
    for route in display:
        lines.extend(_render_route_detail(route))
        lines.append("")
    return "\n".join(lines).rstrip()


# ── Main entry point ──────────────────────────────────────────────────────────

def show_route(
    args: list[str],
    frr: Optional["FRRClient"] = None,
    alias_fn: Optional[Callable[[str], str]] = None,
) -> str:
    """Parse args and produce 'show route' output.

    Args:
        args:     Tokens after 'show route' (e.g. ['detail'], ['10.0.0.0/24'], …)
        frr:      FRRClient instance (or None to use kernel data only)
        alias_fn: Optional callable translating kernel iface names to display names
    """
    if args and args[0].lower() == "advertising-protocol":
        return show_route_adj_protocol(args[1:], frr, advertised=True)
    if args and args[0].lower() == "receive-protocol":
        return show_route_adj_protocol(args[1:], frr, advertised=False)

    detail       = False
    terse        = False
    hidden_only  = False
    prefix_filter: Optional[str] = None
    proto_filter:  Optional[str] = None
    table_filter: Optional[int] = None  # 4 for inet.0, 6 for inet6.0

    i = 0
    while i < len(args):
        tok = args[i].lower()
        if tok == "detail":
            detail = True
        elif tok == "terse":
            terse = True
        elif tok == "hidden":
            hidden_only = True
        elif tok == "table":
            if i + 1 >= len(args):
                return "error: 'table' requires a table name"
            table_name = args[i + 1].lower()
            if table_name == "inet":
                table_filter = 4
            elif table_name == "inet.0":
                table_filter = 4
            elif table_name == "inet6":
                table_filter = 6
            elif table_name == "inet6.0":
                table_filter = 6
            else:
                return f"error: unknown routing table '{args[i + 1]}'"
            i += 1
        elif tok == "protocol":
            if i + 1 >= len(args):
                return "error: 'protocol' requires a protocol name"
            proto_filter = args[i + 1].lower()
            i += 1
        elif _looks_like_prefix(args[i]):
            prefix_filter = args[i]
        else:
            return f"error: unknown option '{args[i]}'"
        i += 1

    # Validate protocol filter early
    if proto_filter is not None and proto_filter not in _PROTO_FILTER_MAP:
        known = ", ".join(sorted(_PROTO_FILTER_MAP))
        return f"error: unknown protocol '{proto_filter}'. Known: {known}"

    # Build the full route table (active + hidden)
    all4, all6 = _build_route_table(frr, alias_fn, detail=detail)

    # Apply table filter first
    if table_filter is not None:
        if table_filter == 4:
            all6 = []
        elif table_filter == 6:
            all4 = []

    # Apply prefix filter to both display and stat sets
    if prefix_filter:
        all4 = [r for r in all4 if r.prefix == prefix_filter]
        all6 = [r for r in all6 if r.prefix == prefix_filter]

    # Apply protocol filter
    if proto_filter:
        proto_name = _PROTO_FILTER_MAP[proto_filter]
        all4 = [r for r in all4 if r.protocol == proto_name]
        all6 = [r for r in all6 if r.protocol == proto_name]

    # Separate display subset from the full stats set
    if hidden_only:
        disp4 = [r for r in all4 if not r.installed]
        disp6 = [r for r in all6 if not r.installed]
    else:
        disp4 = [r for r in all4 if r.installed]
        disp6 = [r for r in all6 if r.installed]

    # Sort by IP address
    disp4 = sorted(disp4, key=lambda r: _sort_key(r.prefix))
    disp6 = sorted(disp6, key=lambda r: _sort_key(r.prefix))

    parts: list[str] = []

    for disp, all_r, tname in ((disp4, all4, "inet.0"), (disp6, all6, "inet6.0")):
        if not all_r and not disp:
            continue
        if detail:
            parts.append(render_detail(disp, tname, all_r))
        elif terse:
            parts.append(render_terse(disp, tname, all_r))
        else:
            parts.append(render_brief(disp, tname, all_r))

    if not parts:
        return "No routes found."

    return "\n\n".join(parts)
