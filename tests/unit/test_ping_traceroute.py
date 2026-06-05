"""Tests for JunOS-style ping / traceroute option parsing and completion."""
from __future__ import annotations

import pytest
from prompt_toolkit.completion import CompleteEvent
from prompt_toolkit.document import Document

from nos.cli.completer import NOSCompleter, _PING_OPTS, _TRACEROUTE_OPTS
from nos.cli.modes.operational import _parse_ping_opts, _parse_traceroute_opts
from nos.cli.parser import CLIMode


# ============================================================================
# Helpers
# ============================================================================

def complete(text: str) -> list[str]:
    """Return completion texts for *text* in operational mode."""
    c = NOSCompleter(mode=CLIMode.OPERATIONAL, edit_path=[], store=None)
    doc = Document(text, len(text))
    return [comp.text for comp in c.get_completions(doc, CompleteEvent())]


def complete_meta(text: str) -> dict[str, str]:
    c = NOSCompleter(mode=CLIMode.OPERATIONAL, edit_path=[], store=None)
    doc = Document(text, len(text))
    return {comp.text: str(comp.display_meta) for comp in c.get_completions(doc, CompleteEvent())}


# ============================================================================
# _parse_ping_opts
# ============================================================================

class TestParsePingOpts:
    def test_no_opts_injects_default_count(self):
        flags, err = _parse_ping_opts([])
        assert err is None
        assert flags == ["-c", "5"]

    def test_count(self):
        flags, err = _parse_ping_opts(["count", "10"])
        assert err is None
        assert "-c" in flags
        assert flags[flags.index("-c") + 1] == "10"
        # explicit count suppresses the default
        assert flags.count("-c") == 1

    def test_count_suppresses_default(self):
        flags, err = _parse_ping_opts(["count", "3"])
        assert err is None
        assert flags == ["-c", "3"]

    def test_size(self):
        flags, err = _parse_ping_opts(["size", "1400"])
        assert err is None
        assert ["-s", "1400"] == flags[-2:]

    def test_interval(self):
        flags, err = _parse_ping_opts(["interval", "2"])
        assert err is None
        assert "-i" in flags
        assert flags[flags.index("-i") + 1] == "2"

    def test_ttl(self):
        flags, err = _parse_ping_opts(["ttl", "64"])
        assert err is None
        assert "-t" in flags
        assert flags[flags.index("-t") + 1] == "64"

    def test_no_resolve(self):
        flags, err = _parse_ping_opts(["no-resolve"])
        assert err is None
        assert "-n" in flags

    def test_do_not_fragment(self):
        flags, err = _parse_ping_opts(["do-not-fragment"])
        assert err is None
        assert "-M" in flags
        assert flags[flags.index("-M") + 1] == "do"

    def test_source(self):
        flags, err = _parse_ping_opts(["source", "10.0.0.1"])
        assert err is None
        assert "-I" in flags
        assert flags[flags.index("-I") + 1] == "10.0.0.1"

    def test_routing_instance_ignored(self):
        flags, err = _parse_ping_opts(["routing-instance", "mgmt"])
        assert err is None
        # routing-instance must not produce any flags
        assert "-c" in flags  # only the default count
        assert len(flags) == 2

    def test_multiple_opts(self):
        flags, err = _parse_ping_opts(["count", "3", "size", "512", "no-resolve"])
        assert err is None
        assert "-c" in flags
        assert flags[flags.index("-c") + 1] == "3"
        assert "-s" in flags
        assert flags[flags.index("-s") + 1] == "512"
        assert "-n" in flags

    # ── error cases ────────────────────────────────────────────────────────

    def test_unknown_option_returns_error(self):
        flags, err = _parse_ping_opts(["bogus"])
        assert err is not None
        assert "bogus" in err

    def test_count_missing_value(self):
        _, err = _parse_ping_opts(["count"])
        assert err is not None
        assert "count" in err

    def test_size_missing_value(self):
        _, err = _parse_ping_opts(["size"])
        assert err is not None

    def test_interval_missing_value(self):
        _, err = _parse_ping_opts(["interval"])
        assert err is not None

    def test_ttl_missing_value(self):
        _, err = _parse_ping_opts(["ttl"])
        assert err is not None

    def test_source_missing_value(self):
        _, err = _parse_ping_opts(["source"])
        assert err is not None

    def test_routing_instance_missing_value(self):
        _, err = _parse_ping_opts(["routing-instance"])
        assert err is not None


# ============================================================================
# _parse_traceroute_opts
# ============================================================================

class TestParseTracerouteOpts:
    def test_no_opts(self):
        flags, err = _parse_traceroute_opts([], "traceroute")
        assert err is None
        assert flags == []

    def test_no_resolve(self):
        for binary in ("traceroute", "tracepath"):
            flags, err = _parse_traceroute_opts(["no-resolve"], binary)
            assert err is None
            assert "-n" in flags

    def test_ttl_traceroute(self):
        flags, err = _parse_traceroute_opts(["ttl", "10"], "traceroute")
        assert err is None
        assert "-m" in flags
        assert flags[flags.index("-m") + 1] == "10"

    def test_ttl_tracepath(self):
        flags, err = _parse_traceroute_opts(["ttl", "10"], "tracepath")
        assert err is None
        assert "-m" in flags

    def test_source_traceroute(self):
        flags, err = _parse_traceroute_opts(["source", "192.168.1.1"], "traceroute")
        assert err is None
        assert "-s" in flags
        assert flags[flags.index("-s") + 1] == "192.168.1.1"

    def test_source_tracepath_skipped(self):
        flags, err = _parse_traceroute_opts(["source", "192.168.1.1"], "tracepath")
        assert err is None
        assert "-s" not in flags

    def test_wait_traceroute(self):
        flags, err = _parse_traceroute_opts(["wait", "3"], "traceroute")
        assert err is None
        assert "-w" in flags
        assert flags[flags.index("-w") + 1] == "3"

    def test_wait_tracepath_skipped(self):
        flags, err = _parse_traceroute_opts(["wait", "3"], "tracepath")
        assert err is None
        assert "-w" not in flags

    def test_as_number_lookup_ignored(self):
        flags, err = _parse_traceroute_opts(["as-number-lookup"], "traceroute")
        assert err is None
        assert flags == []

    def test_multiple_opts(self):
        flags, err = _parse_traceroute_opts(
            ["no-resolve", "ttl", "15", "wait", "2"], "traceroute"
        )
        assert err is None
        assert "-n" in flags
        assert "-m" in flags
        assert "-w" in flags

    # ── error cases ────────────────────────────────────────────────────────

    def test_unknown_option_returns_error(self):
        _, err = _parse_traceroute_opts(["bogus"], "traceroute")
        assert err is not None
        assert "bogus" in err

    def test_ttl_missing_value(self):
        _, err = _parse_traceroute_opts(["ttl"], "traceroute")
        assert err is not None

    def test_source_missing_value(self):
        _, err = _parse_traceroute_opts(["source"], "traceroute")
        assert err is not None

    def test_wait_missing_value(self):
        _, err = _parse_traceroute_opts(["wait"], "traceroute")
        assert err is not None


# ============================================================================
# Completer — ping options
# ============================================================================

class TestPingCompleter:
    def test_host_hint_after_ping_space(self):
        kws = complete("ping ")
        assert kws == []

    def test_host_hint_while_typing_host(self):
        kws = complete("ping 10")
        assert kws == []

    def test_options_offered_after_host(self):
        kws = complete("ping 10.0.0.1 ")
        for opt in _PING_OPTS:
            assert opt in kws, f"expected {opt!r} in completions"

    def test_partial_option_filtered(self):
        kws = complete("ping 10.0.0.1 co")
        assert "count" in kws
        assert "size" not in kws

    def test_value_hint_after_count(self):
        kws = complete("ping 10.0.0.1 count ")
        assert kws == []

    def test_value_hint_after_size(self):
        kws = complete("ping 10.0.0.1 size ")
        assert kws == []

    def test_value_hint_after_source(self):
        kws = complete("ping 10.0.0.1 source ")
        assert kws == []

    def test_count_removed_after_use(self):
        kws = complete("ping 10.0.0.1 count 5 ")
        assert "count" not in kws

    def test_no_resolve_removed_after_use(self):
        kws = complete("ping 10.0.0.1 no-resolve ")
        assert "no-resolve" not in kws

    def test_partial_value_completes_hint(self):
        # Typing "ping 10.0.0.1 count 1" — user has already typed a value, no completions expected
        kws = complete("ping 10.0.0.1 count 1")
        assert kws == []

    def test_multiple_opts_then_new(self):
        kws = complete("ping 10.0.0.1 count 3 no-resolve ")
        assert "count" not in kws
        assert "no-resolve" not in kws
        assert "size" in kws


# ============================================================================
# Completer — traceroute options
# ============================================================================

class TestTracerouteCompleter:
    def test_host_hint_after_traceroute_space(self):
        kws = complete("traceroute ")
        assert kws == []

    def test_host_hint_while_typing_host(self):
        kws = complete("traceroute 192")
        assert kws == []

    def test_options_offered_after_host(self):
        kws = complete("traceroute 10.0.0.1 ")
        for opt in _TRACEROUTE_OPTS:
            assert opt in kws, f"expected {opt!r} in completions"

    def test_partial_option_filtered(self):
        kws = complete("traceroute 10.0.0.1 no")
        assert "no-resolve" in kws
        assert "ttl" not in kws

    def test_value_hint_after_ttl(self):
        kws = complete("traceroute 10.0.0.1 ttl ")
        assert kws == []

    def test_value_hint_after_wait(self):
        kws = complete("traceroute 10.0.0.1 wait ")
        assert kws == []

    def test_ttl_removed_after_use(self):
        kws = complete("traceroute 10.0.0.1 ttl 10 ")
        assert "ttl" not in kws

    def test_no_resolve_removed_after_use(self):
        kws = complete("traceroute 10.0.0.1 no-resolve ")
        assert "no-resolve" not in kws
