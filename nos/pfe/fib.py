"""FIB manager — sends FIB updates to the C PFE process via IPC."""
import ipaddress
import re
from typing import Optional

from nos.pfe.ipc import PFEClient, PFEError
from nos.utils.logger import get_logger

log = get_logger(__name__)

_MAC_RE = re.compile(r"^([0-9a-fA-F]{2}:){5}[0-9a-fA-F]{2}$")
_VLAN_MIN = 1
_VLAN_MAX = 4094


class FIBError(Exception):
    """Raised on FIB operation failures (validation, PFE errors, error replies)."""


class FIBManager:
    """Translates high-level FIB operations into PFE IPC messages."""

    def __init__(self, client: PFEClient) -> None:
        self._client = client

    # ── routes ──────────────────────────────────────────────────────────────

    def route_add(
        self,
        prefix: str,
        nexthop: Optional[str],
        ifindex: int,
    ) -> None:
        _validate_prefix(prefix)
        if nexthop is not None:
            _validate_ip(nexthop, field="nexthop")
        msg: dict = {"type": "fib_add", "prefix": prefix, "ifindex": ifindex}
        if nexthop is not None:
            msg["nexthop"] = nexthop
        log.debug("route_add prefix=%s nexthop=%s ifindex=%d", prefix, nexthop, ifindex)
        self._send(msg)

    def route_del(self, prefix: str) -> None:
        _validate_prefix(prefix)
        log.debug("route_del prefix=%s", prefix)
        self._send({"type": "fib_del", "prefix": prefix})

    # ── neighbours ──────────────────────────────────────────────────────────

    def neigh_add(self, ip: str, mac: str, ifindex: int) -> None:
        _validate_ip(ip, field="ip")
        _validate_mac(mac)
        log.debug("neigh_add ip=%s mac=%s ifindex=%d", ip, mac, ifindex)
        self._send({"type": "neigh_add", "ip": ip, "mac": mac, "ifindex": ifindex})

    def neigh_del(self, ip: str) -> None:
        _validate_ip(ip, field="ip")
        log.debug("neigh_del ip=%s", ip)
        self._send({"type": "neigh_del", "ip": ip})

    # ── VLANs ───────────────────────────────────────────────────────────────

    def vlan_set(self, vlan_id: int, ifindex: int) -> None:
        _validate_vlan(vlan_id)
        log.debug("vlan_set vlan_id=%d ifindex=%d", vlan_id, ifindex)
        self._send({"type": "vlan_set", "vlan_id": vlan_id, "ifindex": ifindex})

    def vlan_del(self, vlan_id: int) -> None:
        _validate_vlan(vlan_id)
        log.debug("vlan_del vlan_id=%d", vlan_id)
        self._send({"type": "vlan_del", "vlan_id": vlan_id})

    # ── internal ─────────────────────────────────────────────────────────────

    def _send(self, msg: dict) -> None:
        """Send *msg* and raise FIBError if the reply indicates failure."""
        try:
            reply = self._client.send_message(msg)
        except PFEError as exc:
            raise FIBError(f"PFE communication error: {exc}") from exc

        if reply.get("status") != "ok":
            err = reply.get("message", "unknown error")
            raise FIBError(f"PFE rejected {msg['type']!r}: {err}")


# ── validators ────────────────────────────────────────────────────────────────

def _validate_prefix(prefix: str) -> None:
    try:
        ipaddress.ip_network(prefix, strict=False)
    except ValueError as exc:
        raise FIBError(f"invalid prefix {prefix!r}: {exc}") from exc


def _validate_ip(addr: str, *, field: str) -> None:
    try:
        ipaddress.ip_address(addr)
    except ValueError as exc:
        raise FIBError(f"invalid {field} address {addr!r}: {exc}") from exc


def _validate_mac(mac: str) -> None:
    if not _MAC_RE.fullmatch(mac):
        raise FIBError(
            f"invalid MAC address {mac!r}: expected colon-separated hex octets "
            f"(e.g. aa:bb:cc:dd:ee:ff)"
        )


def _validate_vlan(vlan_id: int) -> None:
    if not (_VLAN_MIN <= vlan_id <= _VLAN_MAX):
        raise FIBError(
            f"invalid VLAN ID {vlan_id}: must be in range [{_VLAN_MIN}, {_VLAN_MAX}]"
        )
