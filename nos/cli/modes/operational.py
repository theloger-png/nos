"""Operational mode handler for NOS CLI.

Handles all commands available at the operational prompt (>): show, ping,
traceroute, configure.
"""
from __future__ import annotations

import logging
import subprocess
from typing import Optional

try:
    from pyroute2 import IPRoute
except ImportError:  # pragma: no cover
    IPRoute = None  # type: ignore[assignment,misc]

_IFF_LOOPBACK = 0x8   # Linux IFF_LOOPBACK from <net/if.h>
_NUD_PERMANENT = 0x80  # Linux NUD_PERMANENT — static/self FDB entry

# NUD states (kernel neighbour cache)
_NUD_INCOMPLETE = 0x01
_NUD_REACHABLE  = 0x02
_NUD_STALE      = 0x04
_NUD_DELAY      = 0x08
_NUD_PROBE      = 0x10
_NUD_FAILED     = 0x20
_NUD_NOARP      = 0x40
_NUD_ARP_VALID  = _NUD_REACHABLE | _NUD_STALE | _NUD_DELAY | _NUD_PROBE | _NUD_PERMANENT

_NUD_STATE_NAMES: dict[int, str] = {
    _NUD_INCOMPLETE: "incomplete",
    _NUD_REACHABLE:  "reachable",
    _NUD_STALE:      "stale",
    _NUD_DELAY:      "delay",
    _NUD_PROBE:      "probe",
    _NUD_FAILED:     "failed",
    _NUD_NOARP:      "noarp",
    _NUD_PERMANENT:  "permanent",
}

_LOG = logging.getLogger(__name__)

from rich.console import Console
from rich.table import Table
from rich.text import Text

from nos.cli.parser import CLIMode, CommandParser, CommandType, ParseResult, resolve_prefix
from nos.config.serializer import to_set_commands
from nos.config.store import ConfigStore
from nos.pfe.manager import ForwardingMode, PFEManager

console = Console()
_parser = CommandParser()

_SHOW_SUBCMDS: list[str] = [
    "arp", "bgp", "configuration", "ethernet-switching", "forwarding", "interfaces",
    "ipv6", "isis", "route", "system", "vlans",
]


# ============================================================================
# JunOS option parsers for ping / traceroute
# ============================================================================

def _parse_ping_opts(args: list[str]) -> tuple[list[str], Optional[str]]:
    """Translate JunOS ping options into Unix flags.

    Returns ``(flags, error_or_None)``.  The default ``-c 5`` is injected
    unless the caller supplied a ``count`` keyword.
    """
    flags: list[str] = []
    count_set = False
    i = 0
    while i < len(args):
        tok = args[i]
        match tok:
            case "count":
                if i + 1 >= len(args):
                    return [], "ping: 'count' requires a value"
                i += 1
                flags += ["-c", args[i]]
                count_set = True
            case "size":
                if i + 1 >= len(args):
                    return [], "ping: 'size' requires a value"
                i += 1
                flags += ["-s", args[i]]
            case "interval":
                if i + 1 >= len(args):
                    return [], "ping: 'interval' requires a value"
                i += 1
                flags += ["-i", args[i]]
            case "ttl":
                if i + 1 >= len(args):
                    return [], "ping: 'ttl' requires a value"
                i += 1
                flags += ["-t", args[i]]
            case "no-resolve":
                flags.append("-n")
            case "do-not-fragment":
                flags += ["-M", "do"]
            case "source":
                if i + 1 >= len(args):
                    return [], "ping: 'source' requires a value"
                i += 1
                flags += ["-I", args[i]]
            case "routing-instance":
                if i + 1 >= len(args):
                    return [], "ping: 'routing-instance' requires a value"
                i += 1
                _LOG.warning("ping routing-instance %r: not supported in Phase 1; ignored", args[i])
            case _:
                return [], f"ping: unknown option {tok!r}"
        i += 1
    if not count_set:
        flags = ["-c", "5"] + flags
    return flags, None


def _parse_traceroute_opts(
    args: list[str], binary: str
) -> tuple[list[str], Optional[str]]:
    """Translate JunOS traceroute options into Unix flags for *binary*.

    Returns ``(flags, error_or_None)``.  Options unsupported by *binary*
    (i.e. ``tracepath``) are silently skipped with a log warning.
    """
    flags: list[str] = []
    i = 0
    while i < len(args):
        tok = args[i]
        match tok:
            case "no-resolve":
                flags.append("-n")
            case "ttl":
                if i + 1 >= len(args):
                    return [], "traceroute: 'ttl' requires a value"
                i += 1
                flags += ["-m", args[i]]  # both binaries use -m for max hops
            case "source":
                if i + 1 >= len(args):
                    return [], "traceroute: 'source' requires a value"
                i += 1
                if binary == "traceroute":
                    flags += ["-s", args[i]]
                else:
                    _LOG.warning("traceroute source: not supported by %s; ignored", binary)
            case "wait":
                if i + 1 >= len(args):
                    return [], "traceroute: 'wait' requires a value"
                i += 1
                if binary == "traceroute":
                    flags += ["-w", args[i]]
                else:
                    _LOG.warning("traceroute wait: not supported by %s; ignored", binary)
            case "as-number-lookup":
                _LOG.warning("traceroute as-number-lookup: not supported in Phase 1; ignored")
            case _:
                return [], f"traceroute: unknown option {tok!r}"
        i += 1
    return flags, None


class OperationalMode:
    """Execute commands in operational mode."""

    def __init__(self, store: ConfigStore, pfe: Optional[PFEManager] = None) -> None:
        self.store = store
        self._pfe = pfe

    def execute(self, line: str) -> Optional[str]:
        """Parse and execute one command line.

        Returns the rendered output string, or None to signal 'switch to
        configure mode'.  Raises SystemExit on 'exit'.
        """
        result = _parser.parse(line, CLIMode.OPERATIONAL)
        if result.is_error:
            return f"error: {result.error}"
        return self._dispatch(result)

    def _dispatch(self, result: ParseResult) -> Optional[str]:
        match result.command:
            case CommandType.SHOW:
                return self._handle_show(result.args, result.pipe)
            case CommandType.PING:
                return self._handle_ping(result.args)
            case CommandType.TRACEROUTE:
                return self._handle_traceroute(result.args)
            case CommandType.CONFIGURE:
                return None  # Signal: enter configure mode
            case CommandType.EXIT:
                raise SystemExit(0)
            case CommandType.UNKNOWN:
                return f"error: {result.error}"
            case _:
                return f"error: command not valid in operational mode"

    # ------------------------------------------------------------------
    # show
    # ------------------------------------------------------------------

    def _handle_show(self, args: list[str], pipe: Optional[str]) -> str:
        if not args:
            return self._show_help()

        sub_raw = args[0].lower()
        sub_args = args[1:]

        sub, err = resolve_prefix(sub_raw, _SHOW_SUBCMDS)
        if err:
            return f"error: {err}"

        match sub:
            case "arp":
                output = self._show_arp(sub_args)
            case "ipv6":
                output = self._show_ipv6(sub_args)
            case "interfaces":
                output = self._show_interfaces(sub_args)
            case "route":
                output = self._show_route(sub_args)
            case "bgp":
                output = self._show_bgp(sub_args)
            case "isis":
                output = self._show_isis(sub_args)
            case "vlans":
                output = self._show_vlans(sub_args)
            case "system":
                output = self._show_system(sub_args)
            case "forwarding":
                output = self._show_forwarding()
            case "ethernet-switching":
                output = self._show_ethernet_switching(sub_args)
            case "configuration":
                output = self._show_configuration(sub_args)
                return _apply_pipe(output, pipe, self._config_for_display_set(sub_args))
            case _:  # pragma: no cover
                return f"error: unknown show sub-command: {sub!r}"

        return _apply_pipe(output, pipe)

    def _show_help(self) -> str:
        return (
            "Possible completions:\n"
            "  arp                 Show ARP table\n"
            "  ipv6                Show IPv6 information\n"
            "  interfaces          Show interface status and counters\n"
            "  ethernet-switching  Show Ethernet switching table (bridge FDB / MAC table)\n"
            "  route               Show routing table\n"
            "  bgp                 Show BGP information\n"
            "  isis                Show IS-IS information\n"
            "  vlans               Show VLAN table\n"
            "  system              Show system information\n"
            "  forwarding          Show PFE forwarding mode\n"
            "  configuration       Show running configuration (tree format; use | display set for set commands)\n"
        )

    # ------------------------------------------------------------------
    # show arp
    # ------------------------------------------------------------------

    def _read_arp_entries(self) -> Optional[list[dict]]:
        """Read ARP table from the kernel neighbour cache via pyroute2.

        Returns None when pyroute2 is unavailable or a kernel error occurs.
        Skips entries with incomplete/failed/no-arp NUD states.
        """
        if IPRoute is None:
            return None

        try:
            with IPRoute() as ipr:
                links = ipr.get_links()
                neighbours = ipr.get_neighbours(family=2)  # AF_INET
        except Exception as exc:
            _LOG.warning("ARP table read failed (%s)", exc)
            return None

        idx_to_name: dict[int, str] = {}
        for link in links:
            name = link.get_attr("IFLA_IFNAME")
            if name:
                idx_to_name[link["index"]] = name

        entries: list[dict] = []
        for nbr in neighbours:
            if not (nbr["state"] & _NUD_ARP_VALID):
                continue
            ip = nbr.get_attr("NDA_DST")
            mac = nbr.get_attr("NDA_LLADDR")
            if not ip or not mac:
                continue
            ifindex = nbr["ifindex"]
            entries.append({
                "mac": mac,
                "ip": ip,
                "ifname": idx_to_name.get(ifindex, f"if{ifindex}"),
            })
        return entries

    def _render_arp_table(self, entries: list[dict]) -> str:
        header = f"{'MAC Address':<18}{'Address':<16}{'Name':<16}{'Interface':<13}Flags"
        lines = [header]
        for e in sorted(entries, key=lambda x: x["ip"]):
            lines.append(
                f"{e['mac']:<18}{e['ip']:<16}{e['ip']:<16}{e['ifname']:<13}none"
            )
        lines.append(f"Total entries: {len(entries)}")
        return "\n".join(lines)

    def _show_arp(self, args: list[str]) -> str:
        filter_ifname: Optional[str] = None
        filter_ip: Optional[str] = None

        i = 0
        while i < len(args):
            tok = args[i].lower()
            if tok == "interface":
                if i + 1 >= len(args):
                    return "error: 'interface' requires an interface name"
                filter_ifname = args[i + 1]
                i += 2
            elif tok == "hostname":
                if i + 1 >= len(args):
                    return "error: 'hostname' requires an IP address"
                filter_ip = args[i + 1]
                i += 2
            else:
                return f"error: unknown arp option '{args[i]}'"

        entries = self._read_arp_entries()
        if entries is None:
            return "error: could not read ARP table (pyroute2 unavailable or error)"

        if filter_ifname is not None:
            entries = [e for e in entries if e["ifname"] == filter_ifname]
        if filter_ip is not None:
            entries = [e for e in entries if e["ip"] == filter_ip]

        return self._render_arp_table(entries)

    # ------------------------------------------------------------------
    # show ipv6 neighbors
    # ------------------------------------------------------------------

    def _read_ipv6_neighbors(self) -> Optional[list[dict]]:
        """Read IPv6 neighbour table from the kernel via pyroute2 (AF_INET6).

        Returns None when pyroute2 is unavailable or a kernel error occurs.
        NUD_NOARP entries are skipped; all other states are included.
        """
        if IPRoute is None:
            return None

        try:
            with IPRoute() as ipr:
                links = ipr.get_links()
                neighbours = ipr.get_neighbours(family=10)  # AF_INET6
        except Exception as exc:
            _LOG.warning("IPv6 neighbor table read failed (%s)", exc)
            return None

        idx_to_name: dict[int, str] = {}
        for link in links:
            name = link.get_attr("IFLA_IFNAME")
            if name:
                idx_to_name[link["index"]] = name

        entries: list[dict] = []
        for nbr in neighbours:
            state = nbr["state"]
            if state & _NUD_NOARP:
                continue
            ip = nbr.get_attr("NDA_DST")
            if not ip:
                continue
            mac = nbr.get_attr("NDA_LLADDR") or ""
            ifindex = nbr["ifindex"]
            entries.append({
                "ip": ip,
                "mac": mac,
                "ifname": idx_to_name.get(ifindex, f"if{ifindex}"),
                "state": _NUD_STATE_NAMES.get(state, f"0x{state:02x}"),
            })
        return entries

    def _render_ipv6_neighbors(self, entries: list[dict]) -> str:
        header = f"{'IPv6 Address':<41}{'MAC Address':<19}{'Interface':<13}State"
        lines = [header]
        for e in sorted(entries, key=lambda x: x["ip"]):
            lines.append(
                f"{e['ip']:<41}{e['mac']:<19}{e['ifname']:<13}{e['state']}"
            )
        lines.append(f"Total entries: {len(entries)}")
        return "\n".join(lines)

    def _show_ipv6_neighbors(self, args: list[str]) -> str:
        filter_ifname: Optional[str] = None

        i = 0
        while i < len(args):
            tok = args[i].lower()
            if tok == "interface":
                if i + 1 >= len(args):
                    return "error: 'interface' requires an interface name"
                filter_ifname = args[i + 1]
                i += 2
            else:
                return f"error: unknown neighbors option '{args[i]}'"

        entries = self._read_ipv6_neighbors()
        if entries is None:
            return "error: could not read IPv6 neighbor table (pyroute2 unavailable or error)"

        if filter_ifname is not None:
            entries = [e for e in entries if e["ifname"] == filter_ifname]

        return self._render_ipv6_neighbors(entries)

    def _show_ipv6(self, args: list[str]) -> str:
        if not args:
            return "Possible completions:\n  neighbors  Show IPv6 neighbor table\n"
        sub, err = resolve_prefix(args[0].lower(), ["neighbors"])
        if err:
            return f"error: {err}"
        return self._show_ipv6_neighbors(args[1:])

    def _show_interfaces(self, args: list[str]) -> str:
        sub = args[0].lower() if args else ""

        if sub == "terse":
            rows = self._iface_rows()
            if rows is None:
                rows = self._iface_rows_config()
            return self._render_terse(rows)

        if sub == "description":
            rows = self._iface_rows()
            if rows is None:
                rows = self._iface_rows_config()
            return self._render_description(rows)

        # ── verbose format (existing) ──────────────────────────────────────
        if IPRoute is None:
            _LOG.warning("pyroute2 not available; showing config-only interface data")
            return self._show_interfaces_config_only()

        cfg = self.store.get_running()
        ifaces_cfg = cfg.get("interfaces", {})

        try:
            with IPRoute() as ipr:
                links = ipr.get_links()
                addrs = ipr.get_addr()
        except Exception as exc:
            _LOG.warning("kernel interface read failed (%s); showing config-only data", exc)
            return self._show_interfaces_config_only()

        # Build index -> ["addr/prefix", ...] map (IPv4 only)
        addr_map: dict[int, list[str]] = {}
        for addr in addrs:
            if addr["family"] == 2:  # AF_INET
                idx = addr["index"]
                ip = addr.get_attr("IFA_ADDRESS")
                prefix = addr["prefixlen"]
                addr_map.setdefault(idx, []).append(f"{ip}/{prefix}")

        include_lo = "lo" in args

        lines: list[str] = []
        for link in sorted(links, key=lambda l: l.get_attr("IFLA_IFNAME") or ""):
            name = link.get_attr("IFLA_IFNAME")
            if not name:
                continue
            if link["flags"] & _IFF_LOOPBACK and not include_lo:
                continue

            idx = link["index"]
            kernel_mtu = link.get_attr("IFLA_MTU") or 1500
            operstate = (link.get_attr("IFLA_OPERSTATE") or "UNKNOWN").upper()
            link_state = "Up" if operstate == "UP" else "Down"

            cfg_key = name.replace("-", "_")
            iface_cfg = ifaces_cfg.get(cfg_key, {})
            if not isinstance(iface_cfg, dict):
                iface_cfg = {}

            desc = iface_cfg.get("description", "")
            disabled = iface_cfg.get("disable", False)
            state = "Disabled" if disabled else "Enabled"
            mtu = iface_cfg.get("mtu", kernel_mtu)

            lines.append(f"Physical interface: {name}, {state}, Physical link is {link_state}")
            if desc:
                lines.append(f"  Description: {desc}")
            lines.append(f"  Link-level type: Ethernet, MTU: {mtu}")
            for addr_str in addr_map.get(idx, []):
                lines.append(f"  Inet  {addr_str}")
            lines.append("")

        if not lines:
            return "No interfaces found."
        return "\n".join(lines).rstrip()

    def _show_interfaces_config_only(self) -> str:
        """Fallback when pyroute2 is unavailable: show interfaces from running config."""
        cfg = self.store.get_running()
        ifaces = cfg.get("interfaces", {})
        if not ifaces:
            return "No interfaces configured."

        lines: list[str] = []
        for name, data in sorted(ifaces.items()):
            if not isinstance(data, dict):
                continue
            display_name = name.replace("_", "-")
            desc = data.get("description", "")
            disabled = data.get("disable", False)
            state = "Disabled" if disabled else "Enabled"
            lines.append(
                f"Physical interface: {display_name}, {state}, Physical link is Unknown"
            )
            if desc:
                lines.append(f"  Description: {desc}")
            mtu = data.get("mtu", 1500)
            lines.append(f"  Link-level type: Ethernet, MTU: {mtu}")
            inet = (data.get("family") or {}).get("inet") or {}
            for addr in (inet.get("address") or {}):
                lines.append(f"  Inet  {addr}")
            lines.append("")

        return "\n".join(lines).rstrip()

    # ── shared helpers for terse / description ─────────────────────────────

    def _iface_rows(self, include_lo: bool = False) -> "list[dict] | None":
        """Return live kernel interface rows, or None if unavailable/failed."""
        if IPRoute is None:
            return None

        cfg = self.store.get_running()
        ifaces_cfg = cfg.get("interfaces", {})

        try:
            with IPRoute() as ipr:
                links = ipr.get_links()
                addrs = ipr.get_addr()
        except Exception as exc:
            _LOG.warning("kernel interface read failed (%s)", exc)
            return None

        addr_map: dict[int, list[str]] = {}
        for addr in addrs:
            if addr["family"] == 2:  # AF_INET
                idx = addr["index"]
                ip = addr.get_attr("IFA_ADDRESS")
                prefix = addr["prefixlen"]
                addr_map.setdefault(idx, []).append(f"{ip}/{prefix}")

        rows: list[dict] = []
        for link in sorted(links, key=lambda l: l.get_attr("IFLA_IFNAME") or ""):
            name = link.get_attr("IFLA_IFNAME")
            if not name:
                continue
            if link["flags"] & _IFF_LOOPBACK and not include_lo:
                continue

            cfg_key = name.replace("-", "_")
            iface_cfg = ifaces_cfg.get(cfg_key, {})
            if not isinstance(iface_cfg, dict):
                iface_cfg = {}

            disabled = iface_cfg.get("disable", False)
            operstate = (link.get_attr("IFLA_OPERSTATE") or "UNKNOWN").upper()
            rows.append({
                "name": name,
                "admin": "down" if disabled else "up",
                "link": "up" if operstate == "UP" else "down",
                "mtu": iface_cfg.get("mtu", link.get_attr("IFLA_MTU") or 1500),
                "desc": iface_cfg.get("description", ""),
                "addrs": addr_map.get(link["index"], []),
            })
        return rows

    def _iface_rows_config(self) -> list[dict]:
        """Config-only fallback for terse / description when pyroute2 is unavailable."""
        cfg = self.store.get_running()
        ifaces = cfg.get("interfaces", {})
        rows: list[dict] = []
        for name, data in sorted(ifaces.items()):
            if not isinstance(data, dict):
                continue
            display_name = name.replace("_", "-")
            disabled = data.get("disable", False)
            inet = (data.get("family") or {}).get("inet") or {}
            addrs = list(inet.get("address") or {})
            rows.append({
                "name": display_name,
                "admin": "down" if disabled else "up",
                "link": "-",
                "mtu": data.get("mtu", 1500),
                "desc": data.get("description", ""),
                "addrs": addrs,
            })
        return rows

    def _render_terse(self, rows: list[dict]) -> str:
        """Render JunOS-style 'show interfaces terse' output."""
        if not rows:
            return "No interfaces found."
        header = (
            f"{'Interface':<24}{'Admin':<6}{'Link':<5}"
            f"{'Proto':<9}{'Local':<22}Remote"
        )
        lines = [header]
        for row in rows:
            name = row["name"]
            admin = row["admin"]
            link = row["link"]
            # Physical interface row — link is the last field (no padding)
            lines.append(f"{name:<24}{admin:<6}{link}")
            # Logical unit row(s) — one per IPv4 address, JunOS .0 convention
            for i, ip in enumerate(row["addrs"]):
                if i == 0:
                    unit = f"{name}.0"
                    lines.append(
                        f"{unit:<24}{admin:<6}{link:<5}{'inet':<9}{ip}"
                    )
                else:
                    lines.append(f"{'':44}{ip}")
        return "\n".join(lines)

    def _render_description(self, rows: list[dict]) -> str:
        """Render JunOS-style 'show interfaces description' output."""
        if not rows:
            return "No interfaces found."
        header = f"{'Interface':<24}{'Admin':<6}{'Link':<5}Description"
        lines = [header]
        for row in rows:
            line = (
                f"{row['name']:<24}{row['admin']:<6}{row['link']:<5}{row['desc']}"
            ).rstrip()
            lines.append(line)
        return "\n".join(lines)

    def _show_route(self, args: list[str]) -> str:
        return (
            "\ninet.0: (routes from kernel FIB — requires PFE integration)\n\n"
            "  show route is not yet implemented in Phase 1 CLI.\n"
            "  Use 'ip route show' via the shell for now.\n"
        )

    def _show_bgp(self, args: list[str]) -> str:
        sub = args[0].lower() if args else "summary"
        if sub == "summary":
            return (
                "BGP summary information — requires FRR bgpd integration.\n"
                "Use 'vtysh -c \"show bgp summary\"' for current state.\n"
            )
        return f"show bgp {sub}: not yet implemented."

    def _show_isis(self, args: list[str]) -> str:
        sub = args[0].lower() if args else "adjacency"
        return (
            f"show isis {sub} — requires FRR isisd integration.\n"
            "Use 'vtysh -c \"show isis adjacency\"' for current state.\n"
        )

    def _show_vlans(self, args: list[str]) -> str:
        cfg = self.store.get_running()
        vlans = cfg.get("vlans", {})
        if not vlans:
            return "No VLANs configured."

        lines = ["Name             VID    L3-interface"]
        lines.append("-" * 45)
        for name, data in sorted(vlans.items()):
            if not isinstance(data, dict):
                continue
            display_name = name.replace("_", "-")
            vid = data.get("vlan_id", data.get("vlan-id", "—"))
            l3 = data.get("l3_interface", data.get("l3-interface", "—"))
            lines.append(f"{display_name:<17}{str(vid):<7}{l3}")
        return "\n".join(lines)

    def _show_system(self, args: list[str]) -> str:
        cfg = self.store.get_running()
        sys_cfg = cfg.get("system", {})
        hostname = sys_cfg.get("host_name", sys_cfg.get("host-name", "(not set)"))
        domain = sys_cfg.get("domain_name", sys_cfg.get("domain-name", ""))
        lines = [
            f"Hostname:     {hostname}",
        ]
        if domain:
            lines.append(f"Domain:       {domain}")
        return "\n".join(lines)

    def _show_forwarding(self) -> str:
        if IPRoute is None:
            return (
                f"{'Interface':<13}{'Mode':<14}Status\n"
                "(pyroute2 unavailable)"
            )

        try:
            with IPRoute() as ipr:
                links = ipr.get_links()
        except Exception as exc:
            _LOG.warning("kernel interface read failed (%s)", exc)
            return "error: could not read kernel interfaces"

        pfe_active = self._pfe is not None and self._pfe.is_available()
        header = f"{'Interface':<13}{'Mode':<14}Status"
        rows: list[str] = []

        for link in sorted(links, key=lambda l: l.get_attr("IFLA_IFNAME") or ""):
            name = link.get_attr("IFLA_IFNAME")
            if not name:
                continue
            if link["flags"] & _IFF_LOOPBACK:
                continue

            operstate = (link.get_attr("IFLA_OPERSTATE") or "UNKNOWN").upper()
            status = "active" if operstate == "UP" else "inactive"
            mode = (
                self._pfe.detect_forwarding_mode(name)
                if pfe_active
                else ForwardingMode.KERNEL
            )
            rows.append(f"{name:<13}{mode.value:<14}{status}")

        if not rows:
            return header + "\n(no interfaces found)"
        return "\n".join([header] + rows)

    # ------------------------------------------------------------------
    # show ethernet-switching table
    # ------------------------------------------------------------------

    def _build_vlan_id_map(self) -> dict[int, str]:
        """Return {vlan_id: display_name} from the running config."""
        cfg = self.store.get_running()
        vlans = cfg.get("vlans", {})
        result: dict[int, str] = {}
        for name, data in vlans.items():
            if not isinstance(data, dict):
                continue
            vid = data.get("vlan_id") or data.get("vlan-id")
            if vid is not None:
                result[int(vid)] = name.replace("_", "-")
        return result

    def _resolve_vlan_filter(
        self, vlan_str: str, vlan_map: dict[int, str]
    ) -> Optional[int]:
        """Resolve a VLAN name or numeric ID string to an integer VLAN ID.

        Priority: exact name match in config → pure numeric → "vlanNNN" shorthand.
        """
        for vid, name in vlan_map.items():
            if name.lower() == vlan_str.lower():
                return vid
        if vlan_str.isdigit():
            return int(vlan_str)
        lower = vlan_str.lower()
        if lower.startswith("vlan") and lower[4:].isdigit():
            return int(lower[4:])
        return None

    def _read_fdb_entries(self) -> Optional[list[dict]]:
        """Dump nos-br bridge FDB entries via pyroute2.

        Filters out multicast MACs, the all-zeros MAC, and NUD_PERMANENT
        entries (bridge self-entries / statically pinned MACs).

        Returns None if pyroute2 is unavailable or an exception occurs.
        Returns an empty list if the bridge does not exist.
        """
        if IPRoute is None:
            return None

        try:
            with IPRoute() as ipr:
                br_idx_list = ipr.link_lookup(ifname="nos-br")
                if not br_idx_list:
                    return []
                br_idx = br_idx_list[0]

                # Build ifindex → name lookup table for port name resolution.
                links = ipr.get_links()
                idx_to_name: dict[int, str] = {}
                for link in links:
                    name = link.get_attr("IFLA_IFNAME")
                    if name:
                        idx_to_name[link["index"]] = name

                raw = ipr.fdb("dump")

                entries: list[dict] = []
                for e in raw:
                    mac = e.get_attr("NDA_LLADDR")
                    if not mac:
                        continue

                    # Keep only entries whose master is nos-br.
                    if e.get_attr("NDA_MASTER") != br_idx:
                        continue

                    # Skip multicast MACs (first byte has LSB set).
                    try:
                        if int(mac.split(":")[0], 16) & 1:
                            continue
                    except (ValueError, IndexError):
                        continue

                    # Skip all-zeros MAC.
                    if mac == "00:00:00:00:00:00":
                        continue

                    # Skip permanent entries (bridge's own MACs).
                    if e["state"] & _NUD_PERMANENT:
                        continue

                    port_idx = e["ifindex"]
                    entries.append({
                        "mac": mac,
                        "vlan_id": e.get_attr("NDA_VLAN"),
                        "ifname": idx_to_name.get(port_idx, f"if{port_idx}"),
                        "type": "Learn",
                        "age": 0,
                    })

                return entries
        except Exception as exc:
            _LOG.warning("FDB read failed (%s)", exc)
            return None

    def _render_fdb_table(self, entries: list[dict], vlan_map: dict[int, str]) -> str:
        n = len(entries)
        header = f"Ethernet switching table: {n} entries, {n} learned"
        if not entries:
            return header

        col_hdr = (
            f"\n{'VLAN':<12}{'MAC address':<19}{'Type':<10}{'Age':<5}Interfaces"
        )
        lines = [header, col_hdr]
        for e in sorted(entries, key=lambda x: (x.get("vlan_id") or 0, x["mac"])):
            vid = e.get("vlan_id")
            vlan_name = vlan_map.get(vid, f"vlan{vid}") if vid is not None else "default"
            lines.append(
                f"{vlan_name:<12}{e['mac']:<19}{e['type']:<10}{e['age']:<5}{e['ifname']}"
            )
        return "\n".join(lines)

    def _render_fdb_summary(self, entries: list[dict], vlan_map: dict[int, str]) -> str:
        from collections import defaultdict

        vlan_counts: dict[str, int] = defaultdict(int)
        iface_counts: dict[str, int] = defaultdict(int)
        for e in entries:
            vid = e.get("vlan_id")
            vlan_name = vlan_map.get(vid, f"vlan{vid}") if vid is not None else "default"
            vlan_counts[vlan_name] += 1
            iface_counts[e["ifname"]] += 1

        n = len(entries)
        lines = [f"Ethernet switching table: {n} entries, {n} learned", ""]
        lines += [f"{'VLAN':<20}Count", "-" * 30]
        for vname in sorted(vlan_counts):
            lines.append(f"{vname:<20}{vlan_counts[vname]}")
        lines += ["", f"{'Interface':<20}Count", "-" * 30]
        for iface in sorted(iface_counts):
            lines.append(f"{iface:<20}{iface_counts[iface]}")
        return "\n".join(lines)

    def _show_ethernet_switching(self, args: list[str]) -> str:
        if not args or args[0].lower() != "table":
            return "Possible completions:\n  table  Show Ethernet switching table\n"

        sub_args = args[1:]
        filter_ifname: Optional[str] = None
        filter_vlan: Optional[str] = None
        show_summary = False

        i = 0
        while i < len(sub_args):
            tok = sub_args[i].lower()
            if tok == "interface":
                if i + 1 >= len(sub_args):
                    return "error: 'interface' requires an interface name"
                filter_ifname = sub_args[i + 1]
                i += 2
            elif tok == "vlan":
                if i + 1 >= len(sub_args):
                    return "error: 'vlan' requires a VLAN name or ID"
                filter_vlan = sub_args[i + 1]
                i += 2
            elif tok == "summary":
                show_summary = True
                i += 1
            else:
                return f"error: unknown option '{sub_args[i]}'"

        entries = self._read_fdb_entries()
        if entries is None:
            return "error: could not read bridge FDB (pyroute2 unavailable or error)"

        vlan_map = self._build_vlan_id_map()

        if filter_vlan is not None:
            vid = self._resolve_vlan_filter(filter_vlan, vlan_map)
            if vid is None:
                return f"error: unknown VLAN '{filter_vlan}'"
            entries = [e for e in entries if e.get("vlan_id") == vid]

        if filter_ifname is not None:
            entries = [e for e in entries if e["ifname"] == filter_ifname]

        if show_summary:
            return self._render_fdb_summary(entries, vlan_map)

        return self._render_fdb_table(entries, vlan_map)

    def _show_configuration(self, args: list[str]) -> str:
        """Show running config in tree format, optionally filtered to a section."""
        from nos.cli.modes.configure import _get_at_path, render_block

        cfg = self.store.get_running()

        if not args:
            if not cfg:
                return "(empty configuration)"
            return render_block(cfg) or "(empty configuration)"

        section = _get_at_path(cfg, args)
        if section is None:
            return f"(no configuration for '{' '.join(args)}')"
        return render_block(section) or f"(no configuration for '{' '.join(args)}')"

    def _config_for_display_set(self, args: list[str]) -> dict:
        """Return a config dict scoped to *args* suitable for to_set_commands()."""
        from nos.cli.modes.configure import _get_at_path

        cfg = self.store.get_running()
        if not args:
            return cfg
        data = _get_at_path(cfg, args)
        if data is None:
            return {}
        # Wrap the subtree back in its path so to_set_commands produces prefixed output.
        result: object = data
        for tok in reversed(args):
            result = {tok.replace("-", "_"): result}
        return result  # type: ignore[return-value]

    # ------------------------------------------------------------------
    # ping / traceroute
    # ------------------------------------------------------------------

    def _handle_ping(self, args: list[str]) -> str:
        if not args:
            return "error: ping requires a host or IP address"
        target = args[0]
        flags, err = _parse_ping_opts(args[1:])
        if err:
            return f"error: {err}"
        cmd = ["ping"] + flags + [target]
        try:
            proc = subprocess.Popen(
                cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                text=True, bufsize=1,
            )
            try:
                for line in proc.stdout:
                    print(line, end="", flush=True)
            except KeyboardInterrupt:
                proc.terminate()
                proc.wait()
            else:
                proc.wait()
        except FileNotFoundError:
            return "error: ping not found in PATH"
        except Exception as exc:
            return f"error: {exc}"
        return ""

    def _handle_traceroute(self, args: list[str]) -> str:
        if not args:
            return "error: traceroute requires a host or IP address"
        target = args[0]
        opt_args = args[1:]
        for binary in ("traceroute", "tracepath"):
            flags, err = _parse_traceroute_opts(opt_args, binary)
            if err:
                return f"error: {err}"
            try:
                proc = subprocess.Popen(
                    [binary] + flags + [target],
                    stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                    text=True, bufsize=1,
                )
                try:
                    for line in proc.stdout:
                        print(line, end="", flush=True)
                except KeyboardInterrupt:
                    proc.terminate()
                    proc.wait()
                else:
                    proc.wait()
                return ""
            except FileNotFoundError:
                continue
            except Exception as exc:
                return f"error: {exc}"
        return "error: neither traceroute nor tracepath found in PATH"


# ============================================================================
# Pipe filter
# ============================================================================

def _apply_pipe(
    output: str, pipe: Optional[str], config: Optional[dict] = None
) -> str:
    """Apply a JunOS-style pipe filter to *output*.

    *config* is an optional pre-scoped config dict used by ``display set`` to
    regenerate set-commands format from the tree output.
    """
    if not pipe:
        return output

    parts = pipe.strip().split(None, 1)
    verb = parts[0].lower()
    pattern = parts[1] if len(parts) > 1 else ""

    lines = output.splitlines()

    match verb:
        case "match":
            lines = [ln for ln in lines if pattern in ln]
        case "except":
            lines = [ln for ln in lines if pattern not in ln]
        case "find":
            found = False
            result = []
            for ln in lines:
                if not found and pattern in ln:
                    found = True
                if found:
                    result.append(ln)
            lines = result
        case "count":
            return str(len(lines))
        case "no-more":
            pass  # no paging in non-interactive use
        case "display":
            if pattern.strip().lower() == "set" and config is not None:
                cmds = to_set_commands(config)
                return "\n".join(cmds)
        case _:
            pass

    return "\n".join(lines)
