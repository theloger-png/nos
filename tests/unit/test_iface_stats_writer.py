"""Unit tests for nos.pfe.stats.IfaceStatsWriter."""
from __future__ import annotations

import json
import os
import stat
import threading
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from nos.pfe.stats import IfaceStatsWriter


# ---------------------------------------------------------------------------
# Mock helpers
# ---------------------------------------------------------------------------

_IFF_LOOPBACK = 0x8


def _make_link(name: str, flags: int = 0, stats: dict | None = None, operstate: str = "UP") -> MagicMock:
    link = MagicMock()
    link.get_attr.side_effect = lambda k: {
        "IFLA_IFNAME": name,
        "IFLA_OPERSTATE": operstate,
        "IFLA_STATS64": stats or {},
        "IFLA_STATS": None,
    }.get(k)
    link.__getitem__ = MagicMock(side_effect=lambda k: {"flags": flags}[k])
    return link


def _make_iproute(links: list) -> MagicMock:
    instance = MagicMock()
    instance.__enter__ = MagicMock(return_value=instance)
    instance.__exit__ = MagicMock(return_value=False)
    instance.get_links.return_value = links
    return MagicMock(return_value=instance)


_DEFAULT_STATS = {
    "rx_bytes": 1000, "tx_bytes": 2000,
    "rx_packets": 10, "tx_packets": 20,
    "rx_errors": 0, "tx_errors": 0,
    "rx_dropped": 0, "tx_dropped": 0,
    "rx_nohandler": 0,
}


# ---------------------------------------------------------------------------
# _read_raw_stats
# ---------------------------------------------------------------------------

class TestReadRawStats:
    def test_returns_nos_name_and_counters(self, tmp_path):
        writer = IfaceStatsWriter(stats_path=str(tmp_path / "stats.json"))
        links = [_make_link("eth0", stats=_DEFAULT_STATS)]
        with patch("nos.pfe.stats._IPRoute", _make_iproute(links)):
            raw = writer._read_raw_stats(None)
        assert "eth0" in raw
        assert raw["eth0"]["ifInOctets"] == 1000
        assert raw["eth0"]["ifOutOctets"] == 2000
        assert raw["eth0"]["ifInUcastPkts"] == 10
        assert raw["eth0"]["ifOutUcastPkts"] == 20

    def test_skips_loopback(self, tmp_path):
        writer = IfaceStatsWriter(stats_path=str(tmp_path / "stats.json"))
        links = [
            _make_link("lo", flags=_IFF_LOOPBACK, stats=_DEFAULT_STATS),
            _make_link("eth0", stats=_DEFAULT_STATS),
        ]
        with patch("nos.pfe.stats._IPRoute", _make_iproute(links)):
            raw = writer._read_raw_stats(None)
        assert "lo" not in raw
        assert "eth0" in raw

    def test_applies_alias_map(self, tmp_path):
        writer = IfaceStatsWriter(stats_path=str(tmp_path / "stats.json"))
        links = [_make_link("ens34", stats=_DEFAULT_STATS)]
        alias_map = {"ens34": "et1"}
        with patch("nos.pfe.stats._IPRoute", _make_iproute(links)), \
             patch("nos.pfe.stats._ALIAS_AVAILABLE", True), \
             patch("nos.pfe.stats._to_alias", side_effect=lambda n, m: m.get(n, n)):
            raw = writer._read_raw_stats(alias_map)
        assert "et1" in raw
        assert "ens34" not in raw

    def test_no_alias_when_map_is_none(self, tmp_path):
        writer = IfaceStatsWriter(stats_path=str(tmp_path / "stats.json"))
        links = [_make_link("ens34", stats=_DEFAULT_STATS)]
        with patch("nos.pfe.stats._IPRoute", _make_iproute(links)):
            raw = writer._read_raw_stats(None)
        assert "ens34" in raw

    def test_missing_stats64_gives_zeros(self, tmp_path):
        writer = IfaceStatsWriter(stats_path=str(tmp_path / "stats.json"))
        link = MagicMock()
        link.get_attr.side_effect = lambda k: {
            "IFLA_IFNAME": "eth0",
            "IFLA_OPERSTATE": "UP",
            "IFLA_STATS64": None,
            "IFLA_STATS": None,
        }.get(k)
        link.__getitem__ = MagicMock(side_effect=lambda k: {"flags": 0}[k])
        with patch("nos.pfe.stats._IPRoute", _make_iproute([link])):
            raw = writer._read_raw_stats(None)
        assert raw["eth0"]["ifInOctets"] == 0
        assert raw["eth0"]["ifOutOctets"] == 0

    def test_operstate_included(self, tmp_path):
        writer = IfaceStatsWriter(stats_path=str(tmp_path / "stats.json"))
        links = [_make_link("eth0", stats=_DEFAULT_STATS, operstate="DOWN")]
        with patch("nos.pfe.stats._IPRoute", _make_iproute(links)):
            raw = writer._read_raw_stats(None)
        assert raw["eth0"]["_operstate"] == "DOWN"


# ---------------------------------------------------------------------------
# _write_stats
# ---------------------------------------------------------------------------

class TestWriteStats:
    def test_creates_json_file(self, tmp_path):
        writer = IfaceStatsWriter(stats_path=str(tmp_path / "stats.json"))
        writer._write_stats({"timestamp": 1.0, "interval_seconds": 30, "interfaces": {}})
        assert (tmp_path / "stats.json").exists()

    def test_json_structure(self, tmp_path):
        path = tmp_path / "stats.json"
        writer = IfaceStatsWriter(stats_path=str(path))
        payload = {
            "timestamp": 1234567890.0,
            "interval_seconds": 30,
            "interfaces": {"eth0": {"ifInOctets": 42}},
        }
        writer._write_stats(payload)
        with open(path) as fh:
            data = json.load(fh)
        assert data["timestamp"] == 1234567890.0
        assert data["interfaces"]["eth0"]["ifInOctets"] == 42

    def test_atomic_write_no_partial_file_on_success(self, tmp_path):
        writer = IfaceStatsWriter(stats_path=str(tmp_path / "stats.json"))
        writer._write_stats({"timestamp": 1.0, "interval_seconds": 30, "interfaces": {}})
        assert not (tmp_path / "stats.json.tmp").exists()

    def test_file_mode_664(self, tmp_path):
        path = tmp_path / "stats.json"
        writer = IfaceStatsWriter(stats_path=str(path))
        writer._write_stats({"timestamp": 1.0, "interval_seconds": 30, "interfaces": {}})
        mode = stat.S_IMODE(os.stat(path).st_mode)
        assert mode == 0o664

    def test_creates_parent_directory(self, tmp_path):
        deep_path = tmp_path / "a" / "b" / "stats.json"
        writer = IfaceStatsWriter(stats_path=str(deep_path))
        writer._write_stats({"timestamp": 1.0, "interval_seconds": 30, "interfaces": {}})
        assert deep_path.exists()

    def test_write_error_does_not_raise(self, tmp_path):
        writer = IfaceStatsWriter(stats_path="/dev/null/impossible/path/stats.json")
        writer._write_stats({"timestamp": 1.0, "interval_seconds": 30, "interfaces": {}})


# ---------------------------------------------------------------------------
# _collect_once — rate calculation
# ---------------------------------------------------------------------------

class TestCollectOnce:
    def _writer_with_links(self, tmp_path, links):
        writer = IfaceStatsWriter(
            interval=30.0,
            stats_path=str(tmp_path / "stats.json"),
        )
        mock_ipr = _make_iproute(links)
        return writer, mock_ipr

    def test_zero_rates_on_first_sample(self, tmp_path):
        links = [_make_link("eth0", stats=_DEFAULT_STATS)]
        writer, mock_ipr = self._writer_with_links(tmp_path, links)
        with patch("nos.pfe.stats._IPRoute", mock_ipr), \
             patch("nos.pfe.stats._load_alias_map", return_value=None):
            writer._collect_once()
        with open(tmp_path / "stats.json") as fh:
            data = json.load(fh)
        assert data["interfaces"]["eth0"]["bpsIn"] == 0.0
        assert data["interfaces"]["eth0"]["bpsOut"] == 0.0
        assert data["interfaces"]["eth0"]["ppsIn"] == 0.0
        assert data["interfaces"]["eth0"]["ppsOut"] == 0.0

    def test_rates_computed_on_second_sample(self, tmp_path):
        stats1 = dict(_DEFAULT_STATS)
        stats2 = {**stats1, "rx_bytes": 1000 + 3000, "tx_bytes": 2000 + 6000,
                  "rx_packets": 10 + 30, "tx_packets": 20 + 60}

        link1 = _make_link("eth0", stats=stats1)
        link2 = _make_link("eth0", stats=stats2)

        writer = IfaceStatsWriter(interval=30.0, stats_path=str(tmp_path / "stats.json"))

        t0 = time.time()
        with patch("nos.pfe.stats._IPRoute", _make_iproute([link1])), \
             patch("nos.pfe.stats._load_alias_map", return_value=None), \
             patch("nos.pfe.stats.time") as mock_time:
            mock_time.time.return_value = t0
            writer._collect_once()

        with patch("nos.pfe.stats._IPRoute", _make_iproute([link2])), \
             patch("nos.pfe.stats._load_alias_map", return_value=None), \
             patch("nos.pfe.stats.time") as mock_time:
            mock_time.time.return_value = t0 + 30.0
            writer._collect_once()

        with open(tmp_path / "stats.json") as fh:
            data = json.load(fh)

        iface = data["interfaces"]["eth0"]
        # 3000 bytes * 8 bits / 30s = 800 bps
        assert iface["bpsIn"] == pytest.approx(800.0, rel=0.01)
        # 6000 bytes * 8 / 30 = 1600 bps
        assert iface["bpsOut"] == pytest.approx(1600.0, rel=0.01)
        # 30 pkts / 30s = 1.0 pps
        assert iface["ppsIn"] == pytest.approx(1.0, rel=0.01)
        # 60 pkts / 30s = 2.0 pps
        assert iface["ppsOut"] == pytest.approx(2.0, rel=0.01)

    def test_counter_rollback_gives_zero_rate(self, tmp_path):
        """Counter going backwards (e.g., interface reset) must not give negative rates."""
        stats1 = dict(_DEFAULT_STATS)
        stats2 = {**stats1, "rx_bytes": 0}  # counter went backwards

        writer = IfaceStatsWriter(interval=30.0, stats_path=str(tmp_path / "stats.json"))
        t0 = time.time()

        with patch("nos.pfe.stats._IPRoute", _make_iproute([_make_link("eth0", stats=stats1)])), \
             patch("nos.pfe.stats._load_alias_map", return_value=None), \
             patch("nos.pfe.stats.time") as mock_time:
            mock_time.time.return_value = t0
            writer._collect_once()

        with patch("nos.pfe.stats._IPRoute", _make_iproute([_make_link("eth0", stats=stats2)])), \
             patch("nos.pfe.stats._load_alias_map", return_value=None), \
             patch("nos.pfe.stats.time") as mock_time:
            mock_time.time.return_value = t0 + 30.0
            writer._collect_once()

        with open(tmp_path / "stats.json") as fh:
            data = json.load(fh)
        assert data["interfaces"]["eth0"]["bpsIn"] == 0.0

    def test_json_contains_if_mib_fields(self, tmp_path):
        links = [_make_link("eth0", stats=_DEFAULT_STATS)]
        writer = IfaceStatsWriter(stats_path=str(tmp_path / "stats.json"))
        with patch("nos.pfe.stats._IPRoute", _make_iproute(links)), \
             patch("nos.pfe.stats._load_alias_map", return_value=None):
            writer._collect_once()
        with open(tmp_path / "stats.json") as fh:
            data = json.load(fh)
        iface = data["interfaces"]["eth0"]
        for key in ("ifInOctets", "ifOutOctets", "ifInUcastPkts", "ifOutUcastPkts",
                    "ifInErrors", "ifOutErrors", "ifInDiscards", "ifOutDiscards",
                    "ifInUnknownProtos", "bpsIn", "bpsOut", "ppsIn", "ppsOut",
                    "last_updated"):
            assert key in iface, f"missing key: {key}"

    def test_private_operstate_key_not_in_json(self, tmp_path):
        links = [_make_link("eth0", stats=_DEFAULT_STATS)]
        writer = IfaceStatsWriter(stats_path=str(tmp_path / "stats.json"))
        with patch("nos.pfe.stats._IPRoute", _make_iproute(links)), \
             patch("nos.pfe.stats._load_alias_map", return_value=None):
            writer._collect_once()
        with open(tmp_path / "stats.json") as fh:
            data = json.load(fh)
        assert "_operstate" not in data["interfaces"]["eth0"]

    def test_interval_seconds_in_json(self, tmp_path):
        links = [_make_link("eth0", stats=_DEFAULT_STATS)]
        writer = IfaceStatsWriter(interval=30.0, stats_path=str(tmp_path / "stats.json"))
        with patch("nos.pfe.stats._IPRoute", _make_iproute(links)), \
             patch("nos.pfe.stats._load_alias_map", return_value=None):
            writer._collect_once()
        with open(tmp_path / "stats.json") as fh:
            data = json.load(fh)
        assert data["interval_seconds"] == 30.0


# ---------------------------------------------------------------------------
# Flap detection
# ---------------------------------------------------------------------------

class TestFlapDetection:
    def test_no_last_flap_on_first_sample(self, tmp_path):
        links = [_make_link("eth0", stats=_DEFAULT_STATS, operstate="UP")]
        writer = IfaceStatsWriter(stats_path=str(tmp_path / "stats.json"))
        with patch("nos.pfe.stats._IPRoute", _make_iproute(links)), \
             patch("nos.pfe.stats._load_alias_map", return_value=None):
            writer._collect_once()
        with open(tmp_path / "stats.json") as fh:
            data = json.load(fh)
        assert "last_flap" not in data["interfaces"]["eth0"]

    def test_last_flap_recorded_on_state_change(self, tmp_path):
        t0 = time.time()
        writer = IfaceStatsWriter(stats_path=str(tmp_path / "stats.json"))

        with patch("nos.pfe.stats._IPRoute",
                   _make_iproute([_make_link("eth0", stats=_DEFAULT_STATS, operstate="UP")])), \
             patch("nos.pfe.stats._load_alias_map", return_value=None), \
             patch("nos.pfe.stats.time") as mock_time:
            mock_time.time.return_value = t0
            writer._collect_once()

        t1 = t0 + 30.0
        with patch("nos.pfe.stats._IPRoute",
                   _make_iproute([_make_link("eth0", stats=_DEFAULT_STATS, operstate="DOWN")])), \
             patch("nos.pfe.stats._load_alias_map", return_value=None), \
             patch("nos.pfe.stats.time") as mock_time:
            mock_time.time.return_value = t1
            writer._collect_once()

        with open(tmp_path / "stats.json") as fh:
            data = json.load(fh)
        assert "last_flap" in data["interfaces"]["eth0"]
        assert data["interfaces"]["eth0"]["last_flap"] == pytest.approx(t1)

    def test_no_flap_when_state_stable(self, tmp_path):
        t0 = time.time()
        writer = IfaceStatsWriter(stats_path=str(tmp_path / "stats.json"))

        for i, t in enumerate([t0, t0 + 30.0]):
            with patch("nos.pfe.stats._IPRoute",
                       _make_iproute([_make_link("eth0", stats=_DEFAULT_STATS, operstate="UP")])), \
                 patch("nos.pfe.stats._load_alias_map", return_value=None), \
                 patch("nos.pfe.stats.time") as mock_time:
                mock_time.time.return_value = t
                writer._collect_once()

        with open(tmp_path / "stats.json") as fh:
            data = json.load(fh)
        assert "last_flap" not in data["interfaces"]["eth0"]


# ---------------------------------------------------------------------------
# Thread lifecycle
# ---------------------------------------------------------------------------

class TestLifecycle:
    def test_start_creates_daemon_thread(self, tmp_path):
        writer = IfaceStatsWriter(
            interval=9999.0,
            stats_path=str(tmp_path / "stats.json"),
        )
        with patch("nos.pfe.stats._IPRoute", _make_iproute([])), \
             patch("nos.pfe.stats._load_alias_map", return_value=None):
            writer.start()
        try:
            assert writer._thread is not None
            assert writer._thread.daemon
        finally:
            writer.stop()

    def test_start_is_idempotent(self, tmp_path):
        writer = IfaceStatsWriter(
            interval=9999.0,
            stats_path=str(tmp_path / "stats.json"),
        )
        with patch("nos.pfe.stats._IPRoute", _make_iproute([])), \
             patch("nos.pfe.stats._load_alias_map", return_value=None):
            writer.start()
            thread_before = writer._thread
            writer.start()
            assert writer._thread is thread_before
        writer.stop()

    def test_stop_is_idempotent(self, tmp_path):
        writer = IfaceStatsWriter(stats_path=str(tmp_path / "stats.json"))
        writer.stop()   # no thread — must not raise
        writer.stop()

    def test_stop_clears_thread_reference(self, tmp_path):
        writer = IfaceStatsWriter(
            interval=9999.0,
            stats_path=str(tmp_path / "stats.json"),
        )
        with patch("nos.pfe.stats._IPRoute", _make_iproute([])), \
             patch("nos.pfe.stats._load_alias_map", return_value=None):
            writer.start()
        writer.stop()
        assert writer._thread is None

    def test_restarts_after_crash(self, tmp_path):
        """After _collect_once raises, supervisor sleeps and retries."""
        call_count = 0
        stop_after = 2
        collected = threading.Event()

        original_collect = IfaceStatsWriter._collect_once

        def _patched_collect(self_inner):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise RuntimeError("simulated crash")
            collected.set()

        writer = IfaceStatsWriter(
            interval=9999.0,
            stats_path=str(tmp_path / "stats.json"),
        )
        # Override restart delay to make test fast
        with patch.object(type(writer), "_collect_once", _patched_collect), \
             patch("nos.pfe.stats._RESTART_DELAY", 0.05):
            writer.start()
            assert collected.wait(timeout=3.0), "second collection never ran"
        writer.stop()
        assert call_count >= 2
