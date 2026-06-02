"""Stats collector — reads per-interface counters from the C PFE process."""
import dataclasses
import threading
from datetime import datetime, timezone
from typing import Optional

from nos.pfe.ipc import PFEClient, PFEError
from nos.utils.logger import get_logger

log = get_logger(__name__)

_JOIN_TIMEOUT = 5.0  # seconds to wait for the poll thread to exit


class StatsError(Exception):
    """Raised on stats query failures (bad reply, PFE errors, invalid data)."""


@dataclasses.dataclass(frozen=True)
class InterfaceStats:
    ifindex: int
    rx_packets: int
    rx_bytes: int
    tx_packets: int
    tx_bytes: int
    timestamp: datetime


class StatsCollector:
    """Reads per-interface counters from the PFE and optionally caches them
    in a background polling thread."""

    def __init__(self, client: PFEClient) -> None:
        self._client = client
        self._cache: dict[int, InterfaceStats] = {}
        self._cache_lock = threading.Lock()
        self._stop_event = threading.Event()
        self._poll_thread: Optional[threading.Thread] = None

    # ── one-shot queries ─────────────────────────────────────────────────────

    def get_stats(self, ifindex: int) -> InterfaceStats:
        """Query the PFE for a single interface's counters."""
        try:
            reply = self._client.send_message({"type": "stats_get", "ifindex": ifindex})
        except PFEError as exc:
            raise StatsError(f"PFE communication error: {exc}") from exc

        if reply.get("status") != "ok":
            err = reply.get("message", "unknown error")
            raise StatsError(f"stats_get failed for ifindex={ifindex}: {err}")

        data = reply.get("data")
        if not isinstance(data, dict):
            raise StatsError(
                f"stats_get for ifindex={ifindex}: missing or malformed 'data' field"
            )

        return InterfaceStats(
            ifindex=ifindex,
            rx_packets=int(data.get("rx_packets", 0)),
            rx_bytes=int(data.get("rx_bytes", 0)),
            tx_packets=int(data.get("tx_packets", 0)),
            tx_bytes=int(data.get("tx_bytes", 0)),
            timestamp=datetime.now(tz=timezone.utc),
        )

    def get_all_stats(self, ifindexes: list[int]) -> dict[int, InterfaceStats]:
        """Query the PFE for each interface in *ifindexes*.

        Raises StatsError on the first failure; successfully collected stats
        up to that point are discarded.
        """
        result: dict[int, InterfaceStats] = {}
        for ifindex in ifindexes:
            result[ifindex] = self.get_stats(ifindex)
        return result

    # ── cache access ─────────────────────────────────────────────────────────

    def get_cached_stats(self, ifindex: int) -> Optional[InterfaceStats]:
        """Return the most recently polled stats for *ifindex*, or None if
        no poll has completed yet for that interface."""
        with self._cache_lock:
            return self._cache.get(ifindex)

    # ── polling lifecycle ────────────────────────────────────────────────────

    def start_polling(
        self,
        ifindexes: list[int],
        interval: float = 30.0,
    ) -> None:
        """Start a daemon thread that polls *ifindexes* every *interval* seconds.

        The first poll runs immediately when the thread starts.
        Raises StatsError if a poll is already running.
        """
        if self._poll_thread is not None and self._poll_thread.is_alive():
            raise StatsError("polling already running; call stop_polling() first")

        self._stop_event.clear()
        self._poll_thread = threading.Thread(
            target=self._poll_loop,
            args=(list(ifindexes), interval),
            daemon=True,
            name="stats-poller",
        )
        self._poll_thread.start()
        log.info(
            "stats polling started: ifindexes=%s interval=%.1fs", ifindexes, interval
        )

    def stop_polling(self) -> None:
        """Signal the polling thread to stop and wait for it to exit."""
        if self._poll_thread is None:
            return
        self._stop_event.set()
        self._poll_thread.join(timeout=_JOIN_TIMEOUT)
        if self._poll_thread.is_alive():
            log.warning("stats poll thread did not exit within %.1fs", _JOIN_TIMEOUT)
        self._poll_thread = None
        log.info("stats polling stopped")

    # ── internal ─────────────────────────────────────────────────────────────

    def _poll_loop(self, ifindexes: list[int], interval: float) -> None:
        while not self._stop_event.is_set():
            try:
                stats = self.get_all_stats(ifindexes)
                with self._cache_lock:
                    self._cache.update(stats)
                log.debug("polled stats for %d interface(s)", len(stats))
            except StatsError as exc:
                log.warning("stats poll error: %s", exc)
            self._stop_event.wait(timeout=interval)
