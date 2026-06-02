"""PFE manager — single entry point for all PFE operations in NOS."""
import enum
import pathlib
from typing import Optional

from nos.pfe.fib import FIBManager
from nos.pfe.ipc import PFEClient, PFEError
from nos.pfe.stats import StatsCollector, StatsError
from nos.utils.logger import get_logger

log = get_logger(__name__)

_STATS_POLL_INTERVAL = 30.0
_SYS_NET = pathlib.Path("/sys/class/net")


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
        """
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
        self._stats.stop_polling()
        self._client.disconnect()
        self._available = False
        log.info("PFE stopped")

    def is_available(self) -> bool:
        """True if the PFE process was reachable on the last start() call."""
        return self._available

    # ── forwarding mode detection ─────────────────────────────────────────────

    def detect_forwarding_mode(self, ifname: str) -> ForwardingMode:
        """Detect the active forwarding mode for *ifname*.

        Steps:
        1. Returns KERNEL immediately if is_available() is False.
        2. Sends a ping to confirm the PFE is responsive.
        3. Reads /sys/class/net/<ifname>/xdp/ to identify the XDP mode:
             xdp/drv exists  → XDP_NATIVE  (driver / native mode)
             xdp/skb exists  → XDP_GENERIC (generic / SKB mode)
             xdp/ dir exists but no mode subdir → XDP_GENERIC (PFE default)
             no xdp/ dir     → KERNEL
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

        xdp_dir = _SYS_NET / ifname / "xdp"
        if not xdp_dir.is_dir():
            return ForwardingMode.KERNEL
        if (xdp_dir / "drv").exists():
            return ForwardingMode.XDP_NATIVE
        if (xdp_dir / "skb").exists():
            return ForwardingMode.XDP_GENERIC
        # xdp/ dir present but no mode subdir — PFE defaults to generic
        return ForwardingMode.XDP_GENERIC
