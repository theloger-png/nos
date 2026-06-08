"""Unit tests for nos.cli.commands.show.isis."""
from __future__ import annotations

import json
from unittest.mock import MagicMock

import pytest

from nos.cli.commands.show.isis import (
    render_adjacency,
    render_database,
    render_interface,
    render_summary,
    show_isis,
)


# ── Fixtures ──────────────────────────────────────────────────────────────────

ADJ_DATA = {
    "default": {
        "adjacencies": [
            {
                "interface": "eth0",
                "sysId": "rtr02.00",
                "state": "Up",
                "holdtimer": 27,
                "snpa": "aabb.ccdd.eeff",
            },
            {
                "interface": "eth1",
                "sysId": "rtr03.00",
                "state": "Down",
                "holdtimer": 0,
                "snpa": "1122.3344.5566",
            },
        ]
    }
}

DB_DATA = {
    "default": {
        "lsps": [
            {
                "lspId": "rtr01.00-00",
                "seqNumber": 17,
                "checksum": 0xABCD,
                "remainingLifetime": 1199,
                "attached": 1,
                "tlvs": [],
            },
            {
                "lspId": "rtr02.00-00",
                "seqNumber": 9,
                "checksum": 0x1234,
                "remainingLifetime": 800,
                "attached": 0,
                "tlvs": [],
            },
        ]
    }
}

IFACE_DATA = {
    "default": {
        "interfaces": {
            "eth0": {
                "state": "Up",
                "circuitType": "point-to-point",
                "level": "L2",
                "metric": 10,
                "adjacencyCount": 1,
            },
            "lo0": {
                "state": "Up",
                "circuitType": "loopback",
                "level": "L1L2",
                "metric": 0,
                "adjacencyCount": 0,
            },
        }
    }
}

SUMMARY_DATA = {
    "default": {
        "sysId": "0000.0101.0101",
        "isType": "level-2-only",
        "net": "49.0001.0000.0101.0101.00",
        "area": "49.0001",
        "adjacencies": 1,
        "lsps": 2,
    }
}


def _frr(responses: dict) -> MagicMock:
    """Build a mock FRRClient that maps vtysh commands to JSON responses."""
    frr = MagicMock()

    def show_side_effect(cmd: str) -> str:
        for key, val in responses.items():
            if key in cmd:
                return json.dumps(val)
        raise Exception(f"Unexpected FRR command: {cmd!r}")

    frr.show.side_effect = show_side_effect
    return frr


# ── render_adjacency ──────────────────────────────────────────────────────────

class TestRenderAdjacency:
    def test_renders_table(self):
        out = render_adjacency(ADJ_DATA)
        assert "IS-IS instance: default" in out
        assert "eth0" in out
        assert "rtr02.00" in out
        assert "Up" in out
        assert "aabb.ccdd.eeff" in out

    def test_renders_down_adjacency(self):
        out = render_adjacency(ADJ_DATA)
        assert "rtr03.00" in out
        assert "Down" in out

    def test_filter_by_sysid(self):
        out = render_adjacency(ADJ_DATA, filter_id="rtr02")
        assert "rtr02.00" in out
        assert "rtr03.00" not in out

    def test_empty_data(self):
        out = render_adjacency({})
        assert "No IS-IS adjacencies" in out

    def test_no_data_key(self):
        out = render_adjacency({"default": {}})
        assert "No IS-IS adjacencies" in out


# ── render_database ───────────────────────────────────────────────────────────

class TestRenderDatabase:
    def test_renders_table(self):
        out = render_database(DB_DATA)
        assert "IS-IS instance: default" in out
        assert "rtr01.00-00" in out
        assert "rtr02.00-00" in out

    def test_renders_seq_checksum(self):
        out = render_database(DB_DATA)
        assert "0x11" in out  # hex(17)
        assert "0xabcd" in out

    def test_detail_mode(self):
        out = render_database(DB_DATA, detail=True)
        assert "rtr01.00-00" in out
        assert "Sequence" in out
        assert "Lifetime" in out

    def test_empty_database(self):
        out = render_database({})
        assert "empty" in out.lower()


# ── render_interface ──────────────────────────────────────────────────────────

class TestRenderInterface:
    def test_renders_all_interfaces(self):
        out = render_interface(IFACE_DATA)
        assert "eth0" in out
        assert "lo0" in out
        assert "point-to-point" in out

    def test_filter_by_name(self):
        out = render_interface(IFACE_DATA, filter_iface="eth0")
        assert "eth0" in out
        assert "lo0" not in out

    def test_shows_metric_and_level(self):
        out = render_interface(IFACE_DATA)
        assert "L2" in out
        assert "10" in out

    def test_empty_interfaces(self):
        out = render_interface({})
        assert "No IS-IS interfaces" in out


# ── render_summary ────────────────────────────────────────────────────────────

class TestRenderSummary:
    def test_renders_system_id(self):
        out = render_summary(SUMMARY_DATA)
        assert "0000.0101.0101" in out

    def test_renders_net(self):
        out = render_summary(SUMMARY_DATA)
        assert "49.0001.0000.0101.0101.00" in out

    def test_renders_level(self):
        out = render_summary(SUMMARY_DATA)
        assert "level-2-only" in out

    def test_renders_counts(self):
        out = render_summary(SUMMARY_DATA)
        assert "1" in out  # adjacencies
        assert "2" in out  # lsps

    def test_empty_data(self):
        out = render_summary({})
        assert "not running" in out.lower()


# ── show_isis (entry point) ───────────────────────────────────────────────────

class TestShowISIS:
    def test_no_frr_returns_not_running(self):
        out = show_isis([], frr=None)
        assert "not running" in out.lower()

    def test_no_args_shows_adjacency(self):
        frr = _frr({"neighbor": ADJ_DATA})
        out = show_isis([], frr=frr)
        assert "rtr02.00" in out

    def test_adjacency_subcommand(self):
        frr = _frr({"neighbor": ADJ_DATA})
        out = show_isis(["adjacency"], frr=frr)
        assert "rtr02.00" in out
        assert "Up" in out

    def test_adjacency_with_filter(self):
        frr = _frr({"neighbor": ADJ_DATA})
        out = show_isis(["adjacency", "rtr02"], frr=frr)
        assert "rtr02.00" in out
        assert "rtr03.00" not in out

    def test_database_subcommand(self):
        frr = _frr({"database": DB_DATA})
        out = show_isis(["database"], frr=frr)
        assert "rtr01.00-00" in out

    def test_database_detail(self):
        frr = _frr({"database": DB_DATA})
        out = show_isis(["database", "detail"], frr=frr)
        assert "Sequence" in out

    def test_interface_subcommand(self):
        frr = _frr({"interface": IFACE_DATA})
        out = show_isis(["interface"], frr=frr)
        assert "eth0" in out

    def test_interface_with_filter(self):
        frr = _frr({"interface": IFACE_DATA})
        out = show_isis(["interface", "eth0"], frr=frr)
        assert "eth0" in out
        assert "lo0" not in out

    def test_summary_subcommand(self):
        frr = _frr({"summary": SUMMARY_DATA})
        out = show_isis(["summary"], frr=frr)
        assert "0000.0101.0101" in out

    def test_prefix_adj_expands(self):
        frr = _frr({"neighbor": ADJ_DATA})
        out = show_isis(["adj"], frr=frr)
        assert "rtr02.00" in out

    def test_prefix_db_expands(self):
        frr = _frr({"database": DB_DATA})
        out = show_isis(["dat"], frr=frr)
        assert "rtr01.00-00" in out

    def test_unknown_subcommand(self):
        frr = _frr({})
        out = show_isis(["foobar"], frr=frr)
        assert "error" in out.lower()

    def test_frr_failure_adjacency(self):
        frr = MagicMock()
        frr.show.side_effect = Exception("isisd not running")
        out = show_isis(["adjacency"], frr=frr)
        assert "not running" in out.lower()
