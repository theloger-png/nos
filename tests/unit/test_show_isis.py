"""Unit tests for nos.cli.commands.show.isis — FRR 8.x JSON format."""
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


# ── FRR 8.x fixtures ──────────────────────────────────────────────────────────

IFACE_DATA = {
    "areas": [
        {
            "area": "default",
            "circuits": [
                {
                    "circuit": 0,
                    "interface": {
                        "name": "ens34",
                        "circuit-id": "0x0",
                        "state": "Up",
                        "type": "p2p",
                        "level": "L1L2",
                    },
                },
                {
                    "circuit": 0,
                    "interface": {
                        "name": "lo0",
                        "circuit-id": "0x0",
                        "state": "Up",
                        "type": "lan",
                        "level": "L1L2",
                    },
                },
            ],
        }
    ]
}

ADJ_DATA = {
    "areas": [
        {
            "area": "default",
            "circuits": [
                {
                    "circuit": 0,
                    "interface": "ens34",
                    "adjacencies": [
                        {
                            "sysId": "rtr02.00",
                            "state": "Up",
                            "holdtimer": 27,
                            "snpa": "aabb.ccdd.eeff",
                        }
                    ],
                },
                {
                    "circuit": 0,
                    "interface": "lo0",
                },
            ],
        }
    ]
}

DB_DATA = {
    "areas": [
        {
            "area": {"name": "default"},
            "levels": [
                {
                    "id": 1,
                    "lsp": {"id": "nos-dev.00-00", "own": "*"},
                    "pdu-len": 75,
                    "seq-number": "0x00000002",
                    "chksum": "0x68e8",
                    "holdtime": 988,
                    "att-p-ol": "0/0/0",
                    "count": 2,
                },
                {
                    "id": 2,
                    "lsp": {"id": "nos-dev.00-00", "own": "*"},
                    "pdu-len": 75,
                    "seq-number": "0x00000002",
                    "chksum": "0x68e8",
                    "holdtime": 990,
                    "att-p-ol": "0/0/0",
                    "count": 2,
                },
            ],
        }
    ]
}

SUMMARY_DATA = {
    "vrf": "default",
    "process-id": 12345,
    "system-id": "0010.0100.1001",
    "up-time": "3d14h33m",
    "number-areas": 1,
    "areas": [
        {
            "area": "default",
            "net": "49.0001.0010.0100.1001.00",
            "levels": [
                {"id": 1, "last-run-elapsed": "00:03:27"},
                {"id": 2, "last-run-elapsed": "00:03:27"},
            ],
        }
    ],
}


def _frr(responses: dict) -> MagicMock:
    frr = MagicMock()

    def show_side_effect(cmd: str) -> str:
        for key, val in responses.items():
            if key in cmd:
                return json.dumps(val)
        raise Exception(f"Unexpected FRR command: {cmd!r}")

    frr.show.side_effect = show_side_effect
    return frr


# ── render_interface ──────────────────────────────────────────────────────────

class TestRenderInterface:
    def test_renders_both_interfaces(self):
        out = render_interface(IFACE_DATA)
        assert "ens34" in out
        assert "lo0" in out

    def test_renders_state_and_type(self):
        out = render_interface(IFACE_DATA)
        assert "Point to Point" in out
        assert "Passive" in out
        assert "L1/L2 Metric" in out

    def test_filter_by_name(self):
        out = render_interface(IFACE_DATA, filter_iface="ens34")
        assert "ens34" in out
        assert "lo0" not in out

    def test_filter_no_match(self):
        out = render_interface(IFACE_DATA, filter_iface="eth99")
        assert "ens34" not in out
        assert "lo0" not in out

    def test_empty_data(self):
        out = render_interface({})
        assert "No IS-IS interfaces configured" in out

    def test_areas_with_no_circuits(self):
        data = {"areas": [{"area": "default", "circuits": []}]}
        out = render_interface(data)
        assert "No IS-IS interfaces configured" in out

    def test_circuit_without_interface(self):
        data = {"areas": [{"area": "default", "circuits": [{"circuit": 0}]}]}
        out = render_interface(data)
        assert "No IS-IS interfaces configured" in out


# ── render_adjacency ──────────────────────────────────────────────────────────

class TestRenderAdjacency:
    def test_renders_adjacency(self):
        out = render_adjacency(ADJ_DATA)
        assert "ens34" in out
        assert "rtr02.00" in out
        assert "Up" in out
        assert "aabb.ccdd.eeff" in out

    def test_filter_by_sysid(self):
        out = render_adjacency(ADJ_DATA, filter_id="rtr02")
        assert "rtr02.00" in out

    def test_filter_no_match(self):
        out = render_adjacency(ADJ_DATA, filter_id="rtr99")
        # header present but no rows matching
        assert "rtr99" not in out

    def test_no_adjacencies(self):
        data = {"areas": [{"area": "default", "circuits": [{"circuit": 0}]}]}
        out = render_adjacency(data)
        assert "No IS-IS adjacencies" in out

    def test_empty_data(self):
        out = render_adjacency({})
        assert "No IS-IS adjacencies" in out

    def test_alias_fn_translates_interface_names(self):
        def kernel_to_nos(name: str) -> str:
            mapping = {"ens34": "et1", "ens34.101": "et1.101"}
            return mapping.get(name, name)

        out = render_adjacency(ADJ_DATA, alias_fn=kernel_to_nos)
        assert "et1" in out
        assert "ens34" not in out


# ── render_database ───────────────────────────────────────────────────────────

class TestRenderDatabase:
    def test_renders_lsp_entries(self):
        out = render_database(DB_DATA)
        assert "nos-dev.00-00" in out
        assert "0x00000002" in out
        assert "0x68e8" in out

    def test_renders_both_levels(self):
        out = render_database(DB_DATA)
        assert "Level-1" in out
        assert "Level-2" in out

    def test_detail_mode(self):
        out = render_database(DB_DATA, detail=True)
        assert "Sequence" in out
        assert "Checksum" in out
        assert "Lifetime" in out

    def test_empty_database(self):
        out = render_database({})
        assert "empty" in out.lower()


# ── render_summary ────────────────────────────────────────────────────────────

class TestRenderSummary:
    def test_renders_system_id(self):
        out = render_summary(SUMMARY_DATA)
        assert "0010.0100.1001" in out

    def test_renders_net(self):
        out = render_summary(SUMMARY_DATA)
        assert "49.0001.0010.0100.1001.00" in out

    def test_renders_uptime(self):
        out = render_summary(SUMMARY_DATA)
        assert "3d14h33m" in out

    def test_renders_area(self):
        out = render_summary(SUMMARY_DATA)
        assert "default" in out

    def test_empty_data(self):
        out = render_summary({})
        assert "not running" in out.lower()


# ── show_isis (entry point) ───────────────────────────────────────────────────

class TestShowISIS:
    def test_no_frr_returns_not_running(self):
        out = show_isis([], frr=None)
        assert "not running" in out.lower()

    def test_no_args_shows_interfaces(self):
        frr = _frr({"interface": IFACE_DATA})
        out = show_isis([], frr=frr)
        assert "ens34" in out

    def test_interface_subcommand(self):
        frr = _frr({"interface": IFACE_DATA})
        out = show_isis(["interface"], frr=frr)
        assert "ens34" in out
        assert "lo0" in out

    def test_interface_with_filter(self):
        frr = _frr({"interface": IFACE_DATA})
        out = show_isis(["interface", "ens34"], frr=frr)
        assert "ens34" in out
        assert "lo0" not in out

    def test_adjacency_subcommand(self):
        frr = _frr({"neighbor": ADJ_DATA})
        out = show_isis(["adjacency"], frr=frr)
        assert "rtr02.00" in out

    def test_adjacency_with_filter(self):
        frr = _frr({"neighbor": ADJ_DATA})
        out = show_isis(["adjacency", "rtr02"], frr=frr)
        assert "rtr02.00" in out

    def test_database_subcommand(self):
        frr = _frr({"database": DB_DATA})
        out = show_isis(["database"], frr=frr)
        assert "nos-dev.00-00" in out

    def test_database_detail(self):
        frr = _frr({"database": DB_DATA})
        out = show_isis(["database", "detail"], frr=frr)
        assert "Sequence" in out

    def test_summary_subcommand(self):
        frr = _frr({"summary": SUMMARY_DATA})
        out = show_isis(["summary"], frr=frr)
        assert "0010.0100.1001" in out

    def test_prefix_adj_expands(self):
        frr = _frr({"neighbor": ADJ_DATA})
        out = show_isis(["adj"], frr=frr)
        assert "rtr02.00" in out

    def test_prefix_int_expands(self):
        frr = _frr({"interface": IFACE_DATA})
        out = show_isis(["int"], frr=frr)
        assert "ens34" in out

    def test_unknown_subcommand(self):
        frr = _frr({})
        out = show_isis(["foobar"], frr=frr)
        assert "error" in out.lower()

    def test_frr_failure_returns_not_running(self):
        frr = MagicMock()
        frr.show.side_effect = Exception("isisd not running")
        out = show_isis(["interface"], frr=frr)
        assert "not running" in out.lower()
