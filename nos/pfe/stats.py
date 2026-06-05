"""Stats collector — reads per-interface counters from the kernel and C PFE process."""
import dataclasses
import grp
import json
import os
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from nos.pfe.ipc import PFEClient, PFEError
from nos.utils.logger import get_logger

try:
    from pyroute2 import IPRoute as _IPRoute
except ImportError:  # pragma: no cover
    _IPRoute = None  # type: ignore[assignment]

try:
    from nos.utils.interface_alias import load_alias_map as _load_alias_map
    from nos.utils.interface_alias import to_alias as _to_alias
    _ALIAS_AVAILABLE = True
except ImportError:  # pragma: no cover
    _ALIAS_AVAILABLE = False
    _load_alias_map = None  # type: ignore[assignment]
    _to_alias = None  # type: ignore[assignment]

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


# ── IfaceStatsWriter ──────────────────────────────────────────────────────────

_STATS_FILE = "/run/nos/stats.json"
_STATS_FILE_MODE = 0o664
_RESTART_DELAY = 5.0
_IFF_LOOPBACK = 0x8  # Linux IFF_LOOPBACK


def _iface_translate(kernel_name: str, alias_map: "dict[str, str] | None") -> str:
    if alias_map and _ALIAS_AVAILABLE and _to_alias:
        return _to_alias(kernel_name, alias_map)
    return kernel_name


class IfaceStatsWriter:
    """Reads kernel per-interface counters every *interval* seconds and writes
    /run/nos/stats.json.  Rates are 30-second moving averages.

    Interface names in the JSON match the NOS display names: aliases (et0, et1)
    when interface-rename is configured, hardware names otherwise.

    The background thread restarts automatically after a crash (5 s delay).
    """

    def __init__(
        self,
        interval: float = 30.0,
        stats_path: str = _STATS_FILE,
    ) -> None:
        self._interval = interval
        self._stats_path = Path(stats_path)
        self._stop_event = threading.Event()
        self._thread: Optional[threading.Thread] = None
        # Previous sample for rate computation
        self._prev_counters: dict[str, dict[str, int]] = {}
        self._prev_time: Optional[float] = None
        # Operstate change tracking for flap timestamps
        self._prev_operstates: dict[str, str] = {}
        self._last_flap: dict[str, float] = {}

    # ── lifecycle ─────────────────────────────────────────────────────────────

    def start(self) -> None:
        """Start the background writer thread.  Idempotent."""
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._supervisor,
            daemon=True,
            name="iface-stats-writer",
        )
        self._thread.start()
        log.info(
            "iface stats writer started (interval=%.0fs path=%s)",
            self._interval, self._stats_path,
        )

    def stop(self) -> None:
        """Signal the writer thread to stop and wait for it to exit."""
        if self._thread is None:
            return
        self._stop_event.set()
        self._thread.join(timeout=_JOIN_TIMEOUT)
        if self._thread.is_alive():
            log.warning(
                "iface stats writer thread did not exit within %.1fs", _JOIN_TIMEOUT
            )
        self._thread = None
        log.info("iface stats writer stopped")

    # ── internal ─────────────────────────────────────────────────────────────

    def _supervisor(self) -> None:
        """Outer loop: run _collect_once every interval, restart on crash."""
        while not self._stop_event.is_set():
            try:
                self._collect_once()
            except Exception as exc:
                if self._stop_event.is_set():
                    break
                log.error(
                    "iface stats writer crashed: %s — restarting in %.1fs",
                    exc, _RESTART_DELAY,
                )
                self._stop_event.wait(timeout=_RESTART_DELAY)
                continue
            self._stop_event.wait(timeout=self._interval)

    def _collect_once(self) -> None:
        """Read kernel stats, compute rates, write stats.json."""
        if _IPRoute is None:
            return

        now = time.time()
        alias_map: "dict[str, str] | None" = None
        if _ALIAS_AVAILABLE and _load_alias_map:
            try:
                alias_map = _load_alias_map()
            except Exception:
                alias_map = None

        raw = self._read_raw_stats(alias_map)
        if not raw:
            return

        elapsed = (now - self._prev_time) if self._prev_time is not None else 0.0
        ifaces_out: dict[str, dict] = {}
        new_counters: dict[str, dict[str, int]] = {}

        counter_keys = (
            "ifInOctets", "ifOutOctets",
            "ifInUcastPkts", "ifOutUcastPkts",
            "ifInErrors", "ifOutErrors",
            "ifInDiscards", "ifOutDiscards",
            "ifInUnknownProtos",
        )

        for nos_name, d in raw.items():
            operstate = d.get("_operstate", "UNKNOWN")
            prev = self._prev_counters.get(nos_name, {})
            has_prev = bool(prev) and elapsed > 0.0

            counters = {k: d[k] for k in counter_keys}
            new_counters[nos_name] = counters

            def _bps(key: str) -> float:
                if not has_prev:
                    return 0.0
                return max(0.0, (counters[key] - prev.get(key, counters[key])) * 8 / elapsed)

            def _pps(key: str) -> float:
                if not has_prev:
                    return 0.0
                return max(0.0, (counters[key] - prev.get(key, counters[key])) / elapsed)

            entry: dict = {
                **counters,
                "bpsIn":  round(_bps("ifInOctets"),    1),
                "bpsOut": round(_bps("ifOutOctets"),    1),
                "ppsIn":  round(_pps("ifInUcastPkts"),  2),
                "ppsOut": round(_pps("ifOutUcastPkts"), 2),
                "last_updated": now,
            }

            # Flap detection
            prev_state = self._prev_operstates.get(nos_name, "")
            if prev_state and prev_state != operstate:
                self._last_flap[nos_name] = now
            self._prev_operstates[nos_name] = operstate
            if nos_name in self._last_flap:
                entry["last_flap"] = self._last_flap[nos_name]

            ifaces_out[nos_name] = entry

        self._prev_counters = new_counters
        self._prev_time = now

        self._write_stats({
            "timestamp": now,
            "interval_seconds": self._interval,
            "interfaces": ifaces_out,
        })

    def _read_raw_stats(
        self, alias_map: "dict[str, str] | None"
    ) -> "dict[str, dict]":
        """Return {nos_name: counters+operstate} for all non-loopback interfaces."""
        result: dict[str, dict] = {}
        with _IPRoute() as ipr:
            for link in ipr.get_links():
                name = link.get_attr("IFLA_IFNAME")
                if not name:
                    continue
                if link["flags"] & _IFF_LOOPBACK:
                    continue
                nos_name = _iface_translate(name, alias_map)
                s64 = (
                    link.get_attr("IFLA_STATS64")
                    or link.get_attr("IFLA_STATS")
                    or {}
                )
                operstate = (link.get_attr("IFLA_OPERSTATE") or "UNKNOWN").upper()
                result[nos_name] = {
                    "ifInOctets":       int(s64.get("rx_bytes",     0)),
                    "ifOutOctets":      int(s64.get("tx_bytes",     0)),
                    "ifInUcastPkts":    int(s64.get("rx_packets",   0)),
                    "ifOutUcastPkts":   int(s64.get("tx_packets",   0)),
                    "ifInErrors":       int(s64.get("rx_errors",    0)),
                    "ifOutErrors":      int(s64.get("tx_errors",    0)),
                    "ifInDiscards":     int(s64.get("rx_dropped",   0)),
                    "ifOutDiscards":    int(s64.get("tx_dropped",   0)),
                    "ifInUnknownProtos":int(s64.get("rx_nohandler", 0)),
                    "_operstate":       operstate,
                }
        return result

    def _write_stats(self, payload: dict) -> None:
        """Write *payload* atomically to self._stats_path with mode 0664."""
        path = self._stats_path
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            log.warning("cannot create stats dir %s: %s", path.parent, exc)
            return
        tmp = path.parent / (path.name + ".tmp")
        try:
            with open(tmp, "w") as fh:
                json.dump(payload, fh, indent=2)
            os.rename(tmp, path)
            os.chmod(path, _STATS_FILE_MODE)
            try:
                gid = grp.getgrnam("nos").gr_gid
                os.chown(path, -1, gid)
            except (KeyError, PermissionError, OSError):
                pass
        except Exception as exc:
            log.warning("failed to write stats file %s: %s", path, exc)
            try:
                tmp.unlink(missing_ok=True)
            except Exception:
                pass
