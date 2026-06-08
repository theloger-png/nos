"""Unit tests for nos.cli.commands.show.isis — FRR 8.x JSON format."""
from __future__ import annotations

import json
from unittest.mock import MagicMock

import pytest

from nos.cli.commands.show.isis import (
    render_adjacency,
    render_database,
    render_interface,
    render_route,
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

# New FRR 8.4.x format with adjacency data at circuit level
ADJ_DATA_NEW_FORMAT = {
    "areas": [
        {
            "area": "default",
            "circuits": [
                {
                    "circuit": 0,
                },
                {
                    "circuit": 0,
                    "adj": "nos-dev",
                    "interface": "ens34.101",
                    "level": 3,
                    "state": "Up",
                    "expires-in": "28s",
                    "snpa": "2020.2020.2020",
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

ROUTE_TEXT = """\
Area default:
IS-IS L2 IPv4 routing table:
 Prefix       Metric  Interface  Nexthop   Label(s)
 ----------------------------------------------------
 10.0.0.0/24  10      ens34      10.0.0.2  -
 1.1.1.1/32   0       -          -         -
 1.1.1.2/32   10      ens34.101  10.0.0.2  -
"""

DB_TEXT = """\
Area default:
IS-IS Level-1 link state database:
LSP ID                  PduLen  SeqNumber   Chksum  Holdtime  ATT/P/OL
nos-dev.00-00         *    75  0x00000004  0x68e8      1092   0/0/0
rtr02.00-00               75  0x00000003  0x4de8      1093   0/0/0

IS-IS Level-2 link state database:
LSP ID                  PduLen  SeqNumber   Chksum  Holdtime  ATT/P/OL
nos-dev.00-00         *    75  0x00000004  0x68e8      1090   0/0/0
rtr02.00-00               75  0x00000003  0x4de8      1091   0/0/0
"""


def _frr(responses: dict) -> MagicMock:
    frr = MagicMock()

    def show_side_effect(cmd: str) -> str:
        for key, val in responses.items():
            if key in cmd:
                # String values are returned as-is (text output); dicts/lists are JSON-encoded.
                return val if isinstance(val, str) else json.dumps(val)
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

    def test_new_format_with_adj_field(self):
        """Test FRR 8.4.x format with adjacency data at circuit level."""
        out = render_adjacency(ADJ_DATA_NEW_FORMAT)
        assert "ens34.101" in out
        assert "nos-dev" in out
        assert "Up" in out
        assert "2020.2020.2020" in out

    def test_new_format_skips_empty_circuits(self):
        """Test that empty circuits (no 'adj' field) are skipped."""
        out = render_adjacency(ADJ_DATA_NEW_FORMAT)
        # Should find the adjacency
        assert "nos-dev" in out
        # Should not show "No IS-IS adjacencies found"
        assert "No IS-IS adjacencies" not in out

    def test_new_format_with_alias_fn(self):
        """Test that alias_fn translates interface names in new format."""
        def kernel_to_nos(name: str) -> str:
            mapping = {"ens34": "et1", "ens34.101": "et1.101"}
            return mapping.get(name, name)

        out = render_adjacency(ADJ_DATA_NEW_FORMAT, alias_fn=kernel_to_nos)
        assert "et1.101" in out
        assert "ens34" not in out
        assert "nos-dev" in out


# ── render_database ───────────────────────────────────────────────────────────

class TestRenderDatabase:
    def test_renders_lsp_entries(self):
        out = render_database(DB_DATA)
        assert "nos-dev.00-00" in out
        assert "0x2" in out  # Leading zeros stripped
        assert "0x68e8" in out

    def test_renders_both_levels(self):
        out = render_database(DB_DATA)
        assert "level 1" in out  # Lowercase, space separated
        assert "level 2" in out

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


# ── render_route ──────────────────────────────────────────────────────────────

class TestRenderRoute:
    def test_renders_route_entries(self):
        out = render_route(ROUTE_TEXT)
        assert "10.0.0.0/24" in out
        assert "1.1.1.1/32" in out
        assert "1.1.1.2/32" in out

    def test_renders_columns(self):
        out = render_route(ROUTE_TEXT)
        assert "Prefix" in out
        assert "Metric" in out
        assert "Interface" in out
        assert "NH via" in out

    def test_renders_metric_and_interface(self):
        out = render_route(ROUTE_TEXT)
        assert "10.0.0.2" in out
        assert "ens34" in out

    def test_level_and_version(self):
        out = render_route(ROUTE_TEXT, l1_ver=0, l2_ver=4)
        lines = out.split("\n")
        for line in lines:
            if "10.0.0.0/24" in line:
                assert "2" in line   # level
                assert "4" in line   # version (l2_ver)

    def test_empty_routes(self):
        out = render_route("")
        assert "No IS-IS routes" in out
        assert "Current version: L1:0 L2:0" in out

    def test_alias_fn_translates_interface_names(self):
        def kernel_to_nos(name: str) -> str:
            mapping = {"ens34": "et1", "ens34.101": "et1.101"}
            return mapping.get(name, name)

        out = render_route(ROUTE_TEXT, alias_fn=kernel_to_nos)
        assert "et1" in out
        assert "et1.101" in out
        assert "ens34" not in out

    def test_local_route_shows_loopback(self):
        """Local routes (no interface, no nexthop) display lo0.0."""
        out = render_route(ROUTE_TEXT)
        lines = out.split("\n")
        for line in lines:
            if "1.1.1.1/32" in line:
                assert "lo0.0" in line

    def test_version_header_uses_level_versions(self):
        out = render_route(ROUTE_TEXT, l1_ver=3, l2_ver=5)
        assert "Current version: L1:3 L2:5" in out


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

    def test_route_subcommand(self):
        frr = _frr({
            "summary": SUMMARY_DATA,
            "show isis database": DB_TEXT,
            "show isis route": ROUTE_TEXT,
        })
        out = show_isis(["route"], frr=frr)
        assert "10.0.0.0/24" in out
        assert "1.1.1.1/32" in out
        assert "1.1.1.2/32" in out

    def test_route_subcommand_not_running_when_no_summary(self):
        frr = _frr({})
        frr.show.side_effect = Exception("isisd not running")
        out = show_isis(["route"], frr=frr)
        assert "not running" in out.lower()

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
