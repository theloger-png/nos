"""DHCP driver using dnsmasq for NOS.

Manages dnsmasq config files for DHCP server mode and dhclient processes
for DHCP client mode.
"""
from __future__ import annotations

import ipaddress
import os
import re
import signal
import subprocess
from pathlib import Path
from typing import Any, Dict, List, Optional

from nos.utils.logger import get_logger

log = get_logger(__name__)

_CONF_DIR = Path("/etc/dnsmasq.d")
_SERVER_LEASES_FILE = Path("/var/lib/misc/dnsmasq.leases")
_CLIENT_LEASES_FILE = Path("/var/lib/dhcp/dhclient.leases")
_PIDFILE_DIR = Path("/run/nos")


class DnsmasqDriver:
    """Controls dnsmasq for DHCP server and dhclient for DHCP client.

    Paths are injectable for testing.
    """

    def __init__(
        self,
        conf_dir: Path = _CONF_DIR,
        server_leases_file: Path = _SERVER_LEASES_FILE,
        client_leases_file: Path = _CLIENT_LEASES_FILE,
        pidfile_dir: Path = _PIDFILE_DIR,
    ) -> None:
        self._conf_dir = conf_dir
        self._server_leases_file = server_leases_file
        self._client_leases_file = client_leases_file
        self._pidfile_dir = pidfile_dir

    # ── DHCP server ──────────────────────────────────────────────────────────

    def apply(self, config: Dict[str, Any]) -> None:
        """Generate dnsmasq config files from committed config and reload."""
        services = (config.get("system") or {}).get("services") or {}
        dhcp_server = services.get("dhcp_local_server") or {}
        interfaces = dhcp_server.get("interface") or {}
        pools = dhcp_server.get("pool") or {}

        # Remove all existing nos-*.conf files first.
        for f in self._conf_dir.glob("nos-[!b]*.conf"):
            try:
                f.unlink()
                log.debug("Removed dnsmasq config %s", f)
            except OSError as exc:
                log.warning("Could not remove %s: %s", f, exc)

        if not interfaces:
            # No DHCP server config — files removed, reload to clear state.
            self._reload_dnsmasq()
            return

        for iface_name, iface_cfg in interfaces.items():
            pool_names: List[str] = (iface_cfg or {}).get("pool") or []
            if isinstance(pool_names, dict):
                pool_names = [k for k, v in pool_names.items() if v]
            for pool_name in pool_names:
                pool_cfg = pools.get(pool_name) or {}
                content = self._render_pool_conf(iface_name, pool_name, pool_cfg)
                path = self._conf_dir / f"nos-{iface_name}-{pool_name}.conf"
                try:
                    path.write_text(content)
                    log.info("Wrote dnsmasq config %s", path)
                except OSError as exc:
                    log.error("Could not write %s: %s", path, exc)

        self._reload_dnsmasq()

    def _render_pool_conf(
        self, iface: str, pool_name: str, pool_cfg: Dict[str, Any]
    ) -> str:
        """Render a dnsmasq config file for one interface+pool combination."""
        lines: list[str] = [f"# NOS DHCP pool: {pool_name} on {iface}"]

        range_cfg = pool_cfg.get("range") or {}
        low = range_cfg.get("low") if isinstance(range_cfg, dict) else None
        high = range_cfg.get("high") if isinstance(range_cfg, dict) else None
        if low and high:
            lines.append(f"dhcp-range={iface},{low},{high}")

        gateway = pool_cfg.get("gateway")
        if gateway:
            lines.append(f"dhcp-option={iface},3,{gateway}")

        dns_server = pool_cfg.get("dns_server")
        if dns_server:
            lines.append(f"dhcp-option={iface},6,{dns_server}")

        return "\n".join(lines) + "\n"

    def _reload_dnsmasq(self) -> None:
        """Send SIGHUP to dnsmasq to reload config (or use systemctl)."""
        try:
            result = subprocess.run(
                ["systemctl", "reload", "dnsmasq"],
                capture_output=True,
                timeout=10,
            )
            if result.returncode != 0:
                log.warning(
                    "systemctl reload dnsmasq failed (rc=%d): %s",
                    result.returncode,
                    result.stderr.decode(errors="replace"),
                )
        except FileNotFoundError:
            log.debug("systemctl not available; trying SIGHUP")
            self._sighup_dnsmasq()
        except subprocess.TimeoutExpired:
            log.warning("systemctl reload dnsmasq timed out")

    def _sighup_dnsmasq(self) -> None:
        """Send SIGHUP directly to dnsmasq process."""
        try:
            result = subprocess.run(
                ["pidof", "dnsmasq"],
                capture_output=True,
                timeout=5,
            )
            if result.returncode == 0:
                pid = int(result.stdout.split()[0])
                os.kill(pid, signal.SIGHUP)
                log.debug("Sent SIGHUP to dnsmasq (pid %d)", pid)
        except (FileNotFoundError, ValueError, OSError, subprocess.TimeoutExpired) as exc:
            log.warning("Could not SIGHUP dnsmasq: %s", exc)

    # ── DHCP client ──────────────────────────────────────────────────────────

    def apply_client(self, config: Dict[str, Any]) -> None:
        """Start/stop dhclient for interfaces with family inet dhcp enabled."""
        interfaces_cfg = config.get("interfaces") or {}
        dhcp_ifaces: set[str] = set()

        for iface_name, iface_cfg in interfaces_cfg.items():
            cfg = iface_cfg or {}
            inet = cfg.get("family_inet") or {}
            if isinstance(inet, dict) and inet.get("dhcp"):
                dhcp_ifaces.add(iface_name.replace("_", "-"))

        # Stop dhclient on interfaces that no longer need it.
        for pidfile in self._pidfile_dir.glob("dhclient-*.pid"):
            iface = pidfile.stem.replace("dhclient-", "")
            if iface not in dhcp_ifaces:
                self._stop_dhclient(iface)

        # Start dhclient on newly configured interfaces.
        for iface in dhcp_ifaces:
            pidfile = self._pidfile_dir / f"dhclient-{iface}.pid"
            if not self._dhclient_running(pidfile):
                self._start_dhclient(iface, pidfile)

    def _dhclient_running(self, pidfile: Path) -> bool:
        if not pidfile.exists():
            return False
        try:
            pid = int(pidfile.read_text().strip())
            os.kill(pid, 0)
            return True
        except (ValueError, OSError):
            return False

    def _start_dhclient(self, iface: str, pidfile: Path) -> None:
        try:
            subprocess.Popen(
                ["dhclient", "-pf", str(pidfile), iface],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            log.info("Started dhclient on %s", iface)
        except FileNotFoundError:
            log.error("dhclient not found; cannot start DHCP client on %s", iface)
        except OSError as exc:
            log.error("Failed to start dhclient on %s: %s", iface, exc)

    def _stop_dhclient(self, iface: str) -> None:
        pidfile = self._pidfile_dir / f"dhclient-{iface}.pid"
        if self._dhclient_running(pidfile):
            try:
                pid = int(pidfile.read_text().strip())
                os.kill(pid, signal.SIGTERM)
                log.info("Stopped dhclient on %s (pid %d)", iface, pid)
            except (ValueError, OSError) as exc:
                log.warning("Could not stop dhclient on %s: %s", iface, exc)

        # Release the lease.
        try:
            subprocess.run(
                ["dhclient", "-r", iface],
                capture_output=True,
                timeout=10,
            )
            log.info("Released DHCP lease on %s", iface)
        except (FileNotFoundError, subprocess.TimeoutExpired) as exc:
            log.warning("dhclient -r %s failed: %s", iface, exc)

        try:
            pidfile.unlink(missing_ok=True)
        except OSError:
            pass

    # ── Lease file parsing ────────────────────────────────────────────────────

    def parse_server_leases(
        self, iface_filter: Optional[str] = None
    ) -> List[Dict[str, str]]:
        """Parse /var/lib/misc/dnsmasq.leases.

        Format: <expiry-timestamp> <mac> <ip> <hostname> <client-id>

        Returns a list of dicts: expiry, mac, ip, hostname, client_id.
        If iface_filter is given, only leases whose IP falls within a pool
        bound to that interface are returned.
        """
        leases: List[Dict[str, str]] = []
        try:
            text = self._server_leases_file.read_text()
        except (FileNotFoundError, OSError):
            return leases

        for line in text.splitlines():
            parts = line.split()
            if len(parts) < 4:
                continue
            leases.append({
                "expiry": parts[0],
                "mac": parts[1],
                "ip": parts[2],
                "hostname": parts[3],
                "client_id": parts[4] if len(parts) > 4 else "*",
            })

        if iface_filter is not None:
            leases = self._filter_leases_by_iface(leases, iface_filter)

        return leases

    def _filter_leases_by_iface(
        self, leases: List[Dict[str, str]], iface: str
    ) -> List[Dict[str, str]]:
        """Return only leases whose IP belongs to a pool on *iface*."""
        ranges = self._get_iface_ip_ranges(iface)
        if not ranges:
            return leases  # No config info — return all
        result = []
        for lease in leases:
            try:
                ip = ipaddress.ip_address(lease["ip"])
                if any(low <= ip <= high for low, high in ranges):
                    result.append(lease)
            except ValueError:
                pass
        return result

    def _get_iface_ip_ranges(
        self, iface: str
    ) -> List[tuple[ipaddress.IPv4Address, ipaddress.IPv4Address]]:
        """Read pool ranges for *iface* from nos-<iface>-*.conf files."""
        ranges = []
        for f in self._conf_dir.glob(f"nos-{iface}-*.conf"):
            try:
                for line in f.read_text().splitlines():
                    m = re.match(r"dhcp-range=\S+,(\S+),(\S+)$", line)
                    if m:
                        low = ipaddress.ip_address(m.group(1))
                        high = ipaddress.ip_address(m.group(2))
                        ranges.append((low, high))
            except (OSError, ValueError):
                pass
        return ranges

    def server_statistics(
        self, config: Dict[str, Any]
    ) -> List[Dict[str, Any]]:
        """Count active leases per pool.

        Returns list of dicts: pool_name, iface, low, high, active_leases.
        """
        leases = self.parse_server_leases()
        lease_ips = set()
        for lease in leases:
            try:
                lease_ips.add(ipaddress.ip_address(lease["ip"]))
            except ValueError:
                pass

        services = (config.get("system") or {}).get("services") or {}
        dhcp_server = services.get("dhcp_local_server") or {}
        interfaces_cfg = dhcp_server.get("interface") or {}
        pools_cfg = dhcp_server.get("pool") or {}

        stats: List[Dict[str, Any]] = []
        for iface_name, iface_cfg in interfaces_cfg.items():
            pool_names: List[str] = (iface_cfg or {}).get("pool") or []
            if isinstance(pool_names, dict):
                pool_names = [k for k, v in pool_names.items() if v]
            for pool_name in pool_names:
                pool_cfg = pools_cfg.get(pool_name) or {}
                range_cfg = pool_cfg.get("range") or {}
                low_str = range_cfg.get("low") if isinstance(range_cfg, dict) else None
                high_str = range_cfg.get("high") if isinstance(range_cfg, dict) else None

                count = 0
                if low_str and high_str:
                    try:
                        low = ipaddress.ip_address(low_str)
                        high = ipaddress.ip_address(high_str)
                        count = sum(1 for ip in lease_ips if low <= ip <= high)
                    except ValueError:
                        pass

                stats.append({
                    "pool": pool_name,
                    "iface": iface_name.replace("_", "-"),
                    "low": low_str or "—",
                    "high": high_str or "—",
                    "active": count,
                })
        return stats

    def parse_client_leases(self) -> List[Dict[str, str]]:
        """Parse /var/lib/dhcp/dhclient.leases.

        Returns list of dicts: iface, ip, mask, gateway, expiry.
        Only the most recent (last) lease per interface is kept.
        """
        leases: Dict[str, Dict[str, str]] = {}
        try:
            text = self._client_leases_file.read_text()
        except (FileNotFoundError, OSError):
            return []

        current: Dict[str, str] = {}
        for line in text.splitlines():
            line = line.strip()
            m = re.match(r'interface\s+"([^"]+)"\s*;', line)
            if m:
                current["iface"] = m.group(1)
                continue
            m = re.match(r"fixed-address\s+(\S+)\s*;", line)
            if m:
                current["ip"] = m.group(1)
                continue
            m = re.match(r"option subnet-mask\s+(\S+)\s*;", line)
            if m:
                current["mask"] = m.group(1)
                continue
            m = re.match(r"option routers\s+(\S+)\s*;", line)
            if m:
                current["gateway"] = m.group(1)
                continue
            m = re.match(r"expire\s+\d+\s+(.+)\s*;", line)
            if m:
                current["expiry"] = m.group(1).strip()
                continue
            if line == "}":
                iface = current.get("iface")
                if iface:
                    leases[iface] = dict(current)
                current = {}

        return list(leases.values())
