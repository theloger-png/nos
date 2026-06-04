"""JunOS-style 'show bgp' implementation.

Data sources:
  - frr.show("show bgp summary json")      — peer list, AS, router-id
  - frr.show("show bgp neighbor json")     — per-peer detail
  - frr.show("show bgp ipv4 unicast json") — prefix table (for route counts)

Command variants:
  show bgp summary
  show bgp summary detail
  show bgp neighbor
  show bgp neighbor <ip>
"""
from __future__ import annotations

import json
import logging
from typing import Callable, Optional, TYPE_CHECKING

from nos.cli.parser import resolve_prefix

if TYPE_CHECKING:
    from nos.drivers.frr.client import FRRClient

_LOG = logging.getLogger(__name__)

_NOT_RUNNING = "BGP is not running"

# ── Fetch helpers ─────────────────────────────────────────────────────────────

def _frr_fetch(frr: "FRRClient", cmd: str) -> dict:
    """Run a vtysh JSON command; return parsed dict or empty dict on any error."""
    try:
        return json.loads(frr.show(cmd))
    except Exception as exc:
        _LOG.debug("FRR command %r failed: %s", cmd, exc)
        return {}


def _extract_ipv4_summary(data: dict) -> dict:
    """Return the ipv4Unicast summary dict from raw 'show bgp summary json' output."""
    if "ipv4Unicast" in data:
        return data["ipv4Unicast"]
    # Newer FRR wraps in vrfs -> default
    vrf_default = (data.get("vrfs") or {}).get("default") or {}
    if "ipv4Unicast" in vrf_default:
        return vrf_default["ipv4Unicast"]
    return {}


# ── 'show bgp summary' ────────────────────────────────────────────────────────

#  Column header (fixed-width, left-aligned for text, right-aligned for numbers)
_SUMMARY_HDR = (
    f"{'Neighbor':<16}{'V':<5}{'AS':<6}"
    f"{'MsgRcvd':>8}  {'MsgSent':>8}  "
    f"{'InQ':>3}  {'OutQ':>4}  "
    f"{'Up/Down':<9}  State/PfxRcd"
)


def _format_peer_row(
    peer_ip: str,
    version: int,
    remote_as: int,
    msg_rcvd: int,
    msg_sent: int,
    inq: int,
    outq: int,
    uptime: str,
    state_pfx: str,
) -> str:
    return (
        f"{peer_ip:<16}{version:<5}{remote_as:<6}"
        f"{msg_rcvd:>8}  {msg_sent:>8}  "
        f"{inq:>3}  {outq:>4}  "
        f"{uptime:<9}  {state_pfx}"
    )


def render_summary(data: dict, detail: bool = False) -> str:
    """Render 'show bgp summary [detail]' from raw FRR summary JSON."""
    ipv4 = _extract_ipv4_summary(data)
    router_id = ipv4.get("routerId", "0.0.0.0")
    local_as = ipv4.get("as", 0)
    peers: dict = ipv4.get("peers", {})

    lines: list[str] = [
        "BGP summary information for VRF default",
        f"Router identifier {router_id}, local AS number {local_as}",
        "",
    ]

    if not peers:
        lines.append("No BGP neighbors configured.")
        return "\n".join(lines)

    lines.append(_SUMMARY_HDR)

    for peer_ip in sorted(peers):
        peer = peers[peer_ip]
        version = peer.get("version", 4)
        remote_as = peer.get("remoteAs", 0)
        msg_rcvd = peer.get("msgRcvd", 0)
        msg_sent = peer.get("msgSent", 0)
        inq = peer.get("inq", 0)
        outq = peer.get("outq", 0)
        uptime = peer.get("peerUptime", "never")
        state = peer.get("state", "Unknown")
        pfx_rcd = peer.get("pfxRcd", 0)

        state_pfx = str(pfx_rcd) if state == "Established" else state
        lines.append(
            _format_peer_row(peer_ip, version, remote_as, msg_rcvd, msg_sent,
                             inq, outq, uptime, state_pfx)
        )

        if detail:
            established = peer.get("connectionsEstablished", 0)
            dropped = peer.get("connectionsDropped", 0)
            desc = peer.get("desc", "") or peer.get("nbrDesc", "") or ""
            lines.append(f"  Description: {desc or '(none)'}")
            lines.append(f"  Connections established: {established}  dropped: {dropped}")

    return "\n".join(lines)


# ── 'show bgp neighbor' ───────────────────────────────────────────────────────

def _bgp_type_str(local_as: int, remote_as: int) -> str:
    return "Internal" if local_as == remote_as else "External"


def _nlri_families(nbr: dict) -> list[str]:
    """Return list of NLRI family strings from neighbor capability/AF data."""
    caps = nbr.get("neighborCapabilities") or {}
    af_caps = caps.get("addressFamily") or {}
    af_info = nbr.get("addressFamilyInfo") or {}
    all_afs = set(af_caps) | set(af_info)

    mapping = {
        "ipv4Unicast":  "inet-unicast",
        "ipv6Unicast":  "inet6-unicast",
        "l2vpnEvpn":    "evpn",
        "ipv4Multicast": "inet-multicast",
    }
    result = [mapping[k] for k in sorted(all_afs) if k in mapping]
    return result or ["inet-unicast"]


def _last_state_event(nbr: dict) -> tuple[str, str]:
    """Return (last_state, last_event) pair inferred from neighbor data."""
    state = nbr.get("bgpState", "")
    if state == "Established":
        return "OpenConfirm", "Established"
    if state in ("Active", "Connect"):
        return "Idle", "Start"
    if state == "OpenSent":
        return "Active", "TcpConnection"
    if state == "OpenConfirm":
        return "OpenSent", "OpenReceived"
    return "Idle", "Start"


def _build_options(nbr: dict, local_addr: str) -> str:
    """Build the JunOS-style Options string for a neighbor."""
    opts: list[str] = ["Preference"]
    if local_addr and local_addr not in ("", "0.0.0.0"):
        opts.append("LocalAddress")
    if nbr.get("authenticationEnabled"):
        opts.append("AuthKey")
    opts.extend(["HoldTime", "Keepalive"])
    return " ".join(opts)


def render_neighbor_detail(peer_ip: str, nbr: dict) -> str:
    """Render one BGP peer in JunOS 'show bgp neighbor' detail format."""
    remote_as = nbr.get("remoteAs", 0)
    local_as = nbr.get("localAs", 0)

    local_addr = (
        nbr.get("updateSource")
        or nbr.get("localAddress")
        or nbr.get("nexthop")
        or "0.0.0.0"
    )

    bgp_state = nbr.get("bgpState", "Unknown")
    peer_group = nbr.get("peerGroup", "") or ""
    bgp_type = _bgp_type_str(local_as, remote_as)
    peer_router_id = nbr.get("remoteRouterId", "0.0.0.0")
    local_router_id = nbr.get("localRouterId", "0.0.0.0")

    hold_time_cfg = nbr.get("holdTimeConfigured", 90) or 90
    hold_time_msecs = nbr.get("holdTimeMsecs", 0) or 0
    active_hold_time = hold_time_msecs // 1000 if bgp_state == "Established" else 0

    n_flaps = nbr.get("nbrFlaps", 0) or 0

    last_error = (
        nbr.get("lastNotificationReason")
        or nbr.get("lastResetDueTo")
        or "None"
    )
    if not last_error or last_error in ("", "No Error"):
        last_error = "None"

    last_state, last_event = _last_state_event(nbr)
    options_str = _build_options(nbr, local_addr)
    nlri_families = _nlri_families(nbr)
    nlri_str = " ".join(nlri_families)

    caps = nbr.get("neighborCapabilities") or {}
    rr_cap = caps.get("routeRefresh", "")
    supports_rr = "advertisedAndReceived" in rr_cap or "received" in rr_cap.lower()

    gr_info = nbr.get("gracefulRestartInfo") or {}
    gr_timers = gr_info.get("timers") or {}
    stale_hold = gr_timers.get("configuredRestartTimer", 120)

    af_info = nbr.get("addressFamilyInfo") or {}
    ipv4_af = af_info.get("ipv4Unicast") or {}
    accepted_pfx = ipv4_af.get("acceptedPrefixCounter", 0)
    sent_pfx = ipv4_af.get("sentPrefixCounter", 0)
    suppressed_pfx = 0

    if bgp_state == "Established" and sent_pfx > 0:
        send_state = "in sync"
    else:
        send_state = "not advertising"

    I = "  "
    lines: list[str] = [
        f"Peer: {peer_ip} AS {remote_as} Local: {local_addr} AS {local_as}",
        f"{I}Group: {peer_group:<36}Routing-Instance: master",
        f"{I}Forwarding routing-instance: master",
        f"{I}Type: {bgp_type:<15}State: {bgp_state:<16}Flags: <>",
        f"{I}Last State: {last_state:<14}Last Event: {last_event}",
        f"{I}Last Error: {last_error}",
        f"{I}Options: <{options_str}>",
        f"{I}Local Address: {local_addr} Holdtime: {hold_time_cfg} Preference: 170",
        f"{I}Number of flaps: {n_flaps}",
        f"{I}Peer ID: {peer_router_id}  Local ID: {local_router_id}  Active Holdtime: {active_hold_time}",
        f"{I}NLRI for restart configured on peer: {nlri_str}",
        f"{I}NLRI advertised by peer: {nlri_str}",
        f"{I}NLRI for this session: {nlri_str}",
    ]

    if supports_rr:
        lines.append(f"{I}Peer supports Refresh Capability (2)")

    lines.extend([
        f"{I}Stale-route holdtime configured: {stale_hold}",
        f"{I}Table inet.0 Bit: 10000",
        f"{I}RIB State: BGP restart is complete",
        f"{I}Send state: {send_state}",
        f"{I}Active prefixes:              {accepted_pfx}",
        f"{I}Received prefixes:            {accepted_pfx}",
        f"{I}Accepted prefixes:            {accepted_pfx}",
        f"{I}Suppressed due to damping:    {suppressed_pfx}",
        f"{I}Advertised prefixes:          {sent_pfx}",
    ])

    return "\n".join(lines)


# ── Main entry point ──────────────────────────────────────────────────────────

def show_bgp(
    args: list[str],
    frr: Optional["FRRClient"] = None,
    alias_fn: Optional[Callable[[str], str]] = None,
) -> str:
    """Parse args and produce 'show bgp' output.

    Args:
        args:     Tokens after 'show bgp' (e.g. ['summary'], ['neighbor', '10.0.0.2'])
        frr:      FRRClient instance (or None → 'BGP is not running')
        alias_fn: Optional interface name alias translator (unused in BGP views)
    """
    if frr is None:
        return _NOT_RUNNING

    sub_raw = args[0].lower() if args else "summary"
    sub, err = resolve_prefix(sub_raw, ["summary", "neighbor"])
    if err:
        return f"error: {err}"
    rest = args[1:]

    if sub == "summary":
        detail = bool(rest and rest[0].lower() == "detail")
        data = _frr_fetch(frr, "show bgp summary json")
        if not data:
            return _NOT_RUNNING
        return render_summary(data, detail=detail)

    if sub == "neighbor":
        data = _frr_fetch(frr, "show bgp neighbor json")
        if not data:
            return _NOT_RUNNING

        if not rest:
            parts = [
                render_neighbor_detail(ip, data[ip])
                for ip in sorted(data)
                if isinstance(data[ip], dict)
            ]
            return "\n\n".join(parts) if parts else "No BGP neighbors configured."

        peer_ip = rest[0]
        if peer_ip not in data:
            return f"BGP neighbor {peer_ip!r} not found."
        return render_neighbor_detail(peer_ip, data[peer_ip])

    return f"error: unknown 'show bgp' sub-command: {sub!r}"
