"""Operational mode handler for NOS CLI.

Handles all commands available at the operational prompt (>): show, ping,
traceroute, configure.  Show commands are stubs that will be backed by
real kernel / FRR data in later phases.
"""
from __future__ import annotations

import subprocess
from typing import Optional

from rich.console import Console
from rich.table import Table
from rich.text import Text

from nos.cli.parser import CLIMode, CommandParser, CommandType, ParseResult
from nos.config.store import ConfigStore

console = Console()
_parser = CommandParser()


class OperationalMode:
    """Execute commands in operational mode."""

    def __init__(self, store: ConfigStore) -> None:
        self.store = store

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

        sub = args[0].lower()
        sub_args = args[1:]

        match sub:
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
            case _:
                return f"error: unknown show sub-command: {sub!r}"

        return _apply_pipe(output, pipe)

    def _show_help(self) -> str:
        return (
            "Possible completions:\n"
            "  interfaces     Show interface status and counters\n"
            "  route          Show routing table\n"
            "  bgp            Show BGP information\n"
            "  isis           Show IS-IS information\n"
            "  vlans          Show VLAN table\n"
            "  system         Show system information\n"
            "  forwarding     Show PFE forwarding mode\n"
        )

    def _show_interfaces(self, args: list[str]) -> str:
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
                f"Physical interface: {display_name}, {state}, Physical link is Up"
            )
            if desc:
                lines.append(f"  Description: {desc}")
            mtu = data.get("mtu", 1500)
            lines.append(f"  Link-level type: Ethernet, MTU: {mtu}")
            # IPv4 addresses
            inet = (data.get("family") or {}).get("inet") or {}
            for addr in (inet.get("address") or {}):
                lines.append(f"  Inet  {addr}")
            lines.append("")

        return "\n".join(lines).rstrip()

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
        return (
            "Interface    Mode          Status\n"
            "─────────────────────────────────\n"
            "(requires PFE integration for live data)\n"
        )

    # ------------------------------------------------------------------
    # ping / traceroute
    # ------------------------------------------------------------------

    def _handle_ping(self, args: list[str]) -> str:
        if not args:
            return "error: ping requires a host or IP address"
        target = args[0]
        extra = args[1:]
        cmd = ["ping", "-c", "5", target] + extra
        try:
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
            return (result.stdout + result.stderr).rstrip()
        except subprocess.TimeoutExpired:
            return "ping: timed out"
        except FileNotFoundError:
            return "error: ping not found in PATH"
        except Exception as exc:
            return f"error: {exc}"

    def _handle_traceroute(self, args: list[str]) -> str:
        if not args:
            return "error: traceroute requires a host or IP address"
        target = args[0]
        extra = args[1:]
        # Try traceroute then tracepath as fallback
        for binary in ("traceroute", "tracepath"):
            try:
                result = subprocess.run(
                    [binary, target] + extra,
                    capture_output=True, text=True, timeout=60,
                )
                return (result.stdout + result.stderr).rstrip()
            except FileNotFoundError:
                continue
            except subprocess.TimeoutExpired:
                return f"{binary}: timed out"
            except Exception as exc:
                return f"error: {exc}"
        return "error: neither traceroute nor tracepath found in PATH"


# ============================================================================
# Pipe filter
# ============================================================================

def _apply_pipe(output: str, pipe: Optional[str]) -> str:
    """Apply a JunOS-style pipe filter to *output*."""
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
        case _:
            pass

    return "\n".join(lines)
