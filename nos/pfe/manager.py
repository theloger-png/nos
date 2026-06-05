"""PFE manager — single entry point for all PFE operations in NOS."""
import enum
from typing import Optional

from pyroute2 import IPRoute

from nos.pfe.fib import FIBManager
from nos.pfe.ipc import PFEClient, PFEError
from nos.pfe.stats import IfaceStatsWriter, StatsCollector, StatsError
from nos.utils.logger import get_logger

log = get_logger(__name__)

_STATS_POLL_INTERVAL = 30.0
_XDP_ATTACHED_DRV = "xdpdrv"
_XDP_ATTACHED_SKB = "xdpgeneric"


class ForwardingMode(enum.Enum):
    XDP_NATIVE = "xdp-native"
    XDP_GENERIC = "xdp-generic"
    KERNEL = "kernel"


class PFEManager:
    """Owns the PFEClient, FIBManager, and StatsCollector.

    start() is non-fatal — if the PFE socket is unreachable the manager
    enters kernel-only mode and is_available() returns False.
    Callers access sub-managers via pfe.fib.* and pfe.stats.*.
    """

    def __init__(self) -> None:
        self._client = PFEClient()
        self._fib = FIBManager(self._client)
        self._stats = StatsCollector(self._client)
        self._iface_stats_writer = IfaceStatsWriter()
        self._available: bool = False

    # ── properties ───────────────────────────────────────────────────────────

    @property
    def fib(self) -> FIBManager:
        return self._fib

    @property
    def stats(self) -> StatsCollector:
        return self._stats

    # ── lifecycle ─────────────────────────────────────────────────────────────

    def start(self, poll_ifindexes: Optional[list[int]] = None) -> None:
        """Connect to the PFE socket and optionally start stats polling.

        Non-fatal: if the PFE socket is not reachable, a warning is logged
        and the manager continues in kernel-only mode (is_available() == False).
        The kernel stats writer starts unconditionally.
        """
        self._iface_stats_writer.start()

        try:
            self._client.connect()
            self._available = True
            log.info("PFE connected and available")
        except PFEError as exc:
            log.warning("PFE unavailable: %s — kernel-only mode active", exc)
            self._available = False
            return

        if poll_ifindexes:
            try:
                self._stats.start_polling(
                    poll_ifindexes, interval=_STATS_POLL_INTERVAL
                )
            except StatsError as exc:
                log.warning("stats polling could not start: %s", exc)

    def stop(self) -> None:
        """Stop stats polling and disconnect from the PFE."""
        self._iface_stats_writer.stop()
        self._stats.stop_polling()
        self._client.disconnect()
        self._available = False
        log.info("PFE stopped")

    def is_available(self) -> bool:
        """True if the PFE process was reachable on the last start() call."""
        return self._available

    # ── port VLAN operations ──────────────────────────────────────────────────

    def port_vlan_set(self, ifindex: int, vlan_id: int, mode: int) -> None:
        """Push ifindex/vlan_id/mode into the XDP port_vlan_map.

        mode: 0 = access, 1 = trunk.  No-op if PFE is unavailable.
        Raises PFEError on map update failure.
        """
        if not self._available:
            return
        self._client.port_vlan_set(ifindex, vlan_id, mode)

    # ── forwarding mode detection ─────────────────────────────────────────────

    def detect_forwarding_mode(self, ifname: str) -> ForwardingMode:
        """Detect the active forwarding mode for *ifname*.

        1. Returns KERNEL immediately if is_available() is False.
        2. Sends a ping to confirm the PFE is responsive.
        3. Uses pyroute2 IPRoute to query the IFLA_XDP link attribute:
             'xdpdrv'     → XDP_NATIVE
             'xdpgeneric' → XDP_GENERIC
             'none' or no IFLA_XDP → KERNEL
        """
        if not self._available:
            return ForwardingMode.KERNEL

        try:
            reply = self._client.send_message({"type": "ping"})
            if reply.get("status") != "ok":
                log.warning("PFE ping non-ok for ifname=%s", ifname)
                return ForwardingMode.KERNEL
        except PFEError as exc:
            log.warning("PFE ping failed for ifname=%s: %s", ifname, exc)
            return ForwardingMode.KERNEL

        try:
            with IPRoute() as ip:
                links = ip.link("get", ifname=ifname)
                if not links:
                    return ForwardingMode.KERNEL
                xdp = links[0].get_attr("IFLA_XDP")
                if xdp is None:
                    return ForwardingMode.KERNEL
                attached = xdp.get_attr("XDP_ATTACHED")
                if attached == _XDP_ATTACHED_DRV:
                    return ForwardingMode.XDP_NATIVE
                if attached == _XDP_ATTACHED_SKB:
                    return ForwardingMode.XDP_GENERIC
                return ForwardingMode.KERNEL
        except Exception as exc:
            log.warning("XDP detection failed for ifname=%s: %s", ifname, exc)
            return ForwardingMode.KERNEL
