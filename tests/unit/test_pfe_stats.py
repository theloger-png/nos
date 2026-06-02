"""Unit tests for nos.pfe.stats.StatsCollector."""
import threading
from datetime import datetime, timezone
from unittest.mock import MagicMock, call, patch

import pytest

from nos.pfe.stats import InterfaceStats, StatsCollector, StatsError
from nos.pfe.ipc import PFEError


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

_DATA = {"rx_packets": 100, "rx_bytes": 9000, "tx_packets": 50, "tx_bytes": 4500}


def _ok_reply(data: dict | None = None) -> dict:
    return {"status": "ok", "data": data if data is not None else _DATA}


def _err_reply(message: str = "interface not found") -> dict:
    return {"status": "error", "message": message}


def _collector(reply: dict | None = None) -> tuple[StatsCollector, MagicMock]:
    client = MagicMock()
    client.send_message.return_value = reply if reply is not None else _ok_reply()
    return StatsCollector(client), client


# ---------------------------------------------------------------------------
# get_stats — happy path
# ---------------------------------------------------------------------------

class TestGetStats:
    def test_sends_correct_message(self):
        col, client = _collector()
        col.get_stats(3)
        client.send_message.assert_called_once_with({"type": "stats_get", "ifindex": 3})

    def test_returns_interface_stats_dataclass(self):
        col, _ = _collector()
        result = col.get_stats(3)
        assert isinstance(result, InterfaceStats)

    def test_parses_counters(self):
        col, _ = _collector()
        result = col.get_stats(3)
        assert result.ifindex == 3
        assert result.rx_packets == 100
        assert result.rx_bytes == 9000
        assert result.tx_packets == 50
        assert result.tx_bytes == 4500

    def test_timestamp_is_utc(self):
        col, _ = _collector()
        result = col.get_stats(1)
        assert result.timestamp.tzinfo == timezone.utc

    def test_timestamp_is_recent(self):
        before = datetime.now(tz=timezone.utc)
        col, _ = _collector()
        result = col.get_stats(1)
        after = datetime.now(tz=timezone.utc)
        assert before <= result.timestamp <= after

    def test_float_counters_cast_to_int(self):
        """C side sends counters as JSON numbers (doubles)."""
        reply = _ok_reply({"rx_packets": 1.0, "rx_bytes": 2.0,
                           "tx_packets": 3.0, "tx_bytes": 4.0})
        col, _ = _collector(reply)
        result = col.get_stats(1)
        assert type(result.rx_packets) is int
        assert type(result.rx_bytes) is int

    def test_missing_counter_defaults_to_zero(self):
        """Partial data dict — missing fields should default to 0."""
        col, _ = _collector(_ok_reply({"rx_packets": 7}))
        result = col.get_stats(1)
        assert result.rx_packets == 7
        assert result.rx_bytes == 0
        assert result.tx_packets == 0
        assert result.tx_bytes == 0

    def test_interface_stats_is_frozen(self):
        col, _ = _collector()
        result = col.get_stats(1)
        with pytest.raises((dataclasses.FrozenInstanceError, AttributeError)):
            result.rx_packets = 999  # type: ignore[misc]

    def test_error_reply_raises_stats_error(self):
        col, _ = _collector(_err_reply("stats_get: interface not found"))
        with pytest.raises(StatsError, match="stats_get failed"):
            col.get_stats(5)

    def test_error_reply_includes_ifindex(self):
        col, _ = _collector(_err_reply())
        with pytest.raises(StatsError, match="ifindex=5"):
            col.get_stats(5)

    def test_pfe_error_wrapped_as_stats_error(self):
        client = MagicMock()
        client.send_message.side_effect = PFEError("socket dead")
        col = StatsCollector(client)
        with pytest.raises(StatsError, match="PFE communication error"):
            col.get_stats(1)

    def test_missing_data_field_raises_stats_error(self):
        col, _ = _collector({"status": "ok"})
        with pytest.raises(StatsError, match="missing or malformed"):
            col.get_stats(1)

    def test_null_data_field_raises_stats_error(self):
        col, _ = _collector({"status": "ok", "data": None})
        with pytest.raises(StatsError, match="missing or malformed"):
            col.get_stats(1)


# ---------------------------------------------------------------------------
# get_all_stats
# ---------------------------------------------------------------------------

class TestGetAllStats:
    def test_queries_each_ifindex(self):
        col, client = _collector()
        col.get_all_stats([1, 2, 3])
        assert client.send_message.call_count == 3
        client.send_message.assert_any_call({"type": "stats_get", "ifindex": 1})
        client.send_message.assert_any_call({"type": "stats_get", "ifindex": 2})
        client.send_message.assert_any_call({"type": "stats_get", "ifindex": 3})

    def test_returns_dict_keyed_by_ifindex(self):
        col, _ = _collector()
        result = col.get_all_stats([1, 2])
        assert set(result.keys()) == {1, 2}
        assert all(isinstance(v, InterfaceStats) for v in result.values())

    def test_each_entry_has_correct_ifindex(self):
        col, _ = _collector()
        result = col.get_all_stats([4, 7])
        assert result[4].ifindex == 4
        assert result[7].ifindex == 7

    def test_empty_list_returns_empty_dict(self):
        col, client = _collector()
        result = col.get_all_stats([])
        assert result == {}
        client.send_message.assert_not_called()

    def test_error_on_first_interface_propagates(self):
        client = MagicMock()
        client.send_message.side_effect = [
            _err_reply("not found"),
            _ok_reply(),
        ]
        col = StatsCollector(client)
        with pytest.raises(StatsError):
            col.get_all_stats([1, 2])

    def test_stops_on_first_error(self):
        """get_all_stats must not query further interfaces after a failure."""
        client = MagicMock()
        client.send_message.side_effect = PFEError("broken")
        col = StatsCollector(client)
        with pytest.raises(StatsError):
            col.get_all_stats([1, 2, 3])
        assert client.send_message.call_count == 1


# ---------------------------------------------------------------------------
# get_cached_stats
# ---------------------------------------------------------------------------

class TestGetCachedStats:
    def test_returns_none_before_any_poll(self):
        col, _ = _collector()
        assert col.get_cached_stats(1) is None

    def test_returns_none_for_unknown_ifindex(self):
        col, _ = _collector()
        col.get_stats(1)          # does NOT update the cache
        assert col.get_cached_stats(99) is None

    def test_returns_stats_after_cache_update(self):
        """Directly populate the internal cache and verify retrieval."""
        col, _ = _collector()
        stats = col.get_stats(1)
        with col._cache_lock:
            col._cache[1] = stats
        assert col.get_cached_stats(1) is stats


# ---------------------------------------------------------------------------
# polling lifecycle
# ---------------------------------------------------------------------------

class TestPolling:
    def _one_shot_poller(self, col: StatsCollector) -> None:
        """Replace _stop_event.wait so the poll loop runs exactly once."""
        original_wait = col._stop_event.wait

        def _fake_wait(timeout: float) -> bool:
            col._stop_event.set()
            return True

        col._stop_event.wait = _fake_wait  # type: ignore[method-assign]

    def test_start_creates_daemon_thread(self):
        col, _ = _collector()
        self._one_shot_poller(col)
        col.start_polling([1])
        assert col._poll_thread is not None
        col._poll_thread.join(timeout=2)
        assert col._poll_thread.daemon

    def test_poll_loop_updates_cache(self):
        col, client = _collector()
        self._one_shot_poller(col)
        col.start_polling([1, 2], interval=60)
        col._poll_thread.join(timeout=2)
        assert col.get_cached_stats(1) is not None
        assert col.get_cached_stats(2) is not None

    def test_poll_loop_calls_send_message_for_each_ifindex(self):
        col, client = _collector()
        self._one_shot_poller(col)
        col.start_polling([10, 20], interval=60)
        col._poll_thread.join(timeout=2)
        calls = [c[0][0]["ifindex"] for c in client.send_message.call_args_list]
        assert 10 in calls and 20 in calls

    def test_poll_error_does_not_stop_thread(self):
        """A stats error on the first iteration must not kill the thread;
        the thread should attempt the second iteration."""
        call_count = 0
        stop_after = 2

        client = MagicMock()

        def _side_effect(msg):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return _err_reply("transient failure")
            return _ok_reply()

        client.send_message.side_effect = _side_effect
        col = StatsCollector(client)

        iteration = 0

        def _fake_wait(timeout: float) -> bool:
            nonlocal iteration
            iteration += 1
            if iteration >= stop_after:
                col._stop_event.set()
            return col._stop_event.is_set()

        col._stop_event.wait = _fake_wait  # type: ignore[method-assign]
        col.start_polling([1], interval=0.01)
        col._poll_thread.join(timeout=2)

        # Two iterations ran — cache is populated from the second one.
        assert col.get_cached_stats(1) is not None

    def test_start_polling_raises_if_already_running(self):
        col, _ = _collector()
        # Don't use one_shot_poller — we want the thread to stay alive.
        col.start_polling([1], interval=9999)
        try:
            with pytest.raises(StatsError, match="already running"):
                col.start_polling([1])
        finally:
            col.stop_polling()

    def test_stop_polling_is_idempotent(self):
        col, _ = _collector()
        col.stop_polling()  # no thread running — should not raise

    def test_stop_polling_joins_thread(self):
        col, _ = _collector()
        self._one_shot_poller(col)
        col.start_polling([1], interval=60)
        col.stop_polling()
        assert col._poll_thread is None

    def test_stop_polling_clears_poll_thread_reference(self):
        col, _ = _collector()
        self._one_shot_poller(col)
        col.start_polling([1])
        col._poll_thread.join(timeout=2)
        col.stop_polling()
        assert col._poll_thread is None


# ---------------------------------------------------------------------------
# import guard for frozen dataclass test
# ---------------------------------------------------------------------------

import dataclasses  # noqa: E402  (intentional late import for clarity)
