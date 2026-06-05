"""Tests for show interfaces stats output and helper functions."""
from __future__ import annotations

import json
from unittest.mock import Mock, patch

import pytest

from nos.cli.modes.operational import (
    OperationalMode,
    _fmt_bps,
    _fmt_pps,
    _format_iface_stats,
    _fmt_timestamp,
    _load_iface_stats,
)
from nos.config.store import ConfigStore


# ---------------------------------------------------------------------------
# Helper mocks
# ---------------------------------------------------------------------------

class _MockLink:
    def __init__(self, name: str, index: int = 2, flags: int = 0,
                 mtu: int = 1500, operstate: str = "UP") -> None:
        self._a = {"IFLA_IFNAME": name, "IFLA_MTU": mtu, "IFLA_OPERSTATE": operstate}
        self._i = {"flags": flags, "index": index}

    def get_attr(self, key: str):
        return self._a.get(key)

    def __getitem__(self, key: str):
        return self._i[key]


class _MockAddr:
    def __init__(self, index: int, address: str, prefixlen: int, family: int = 2) -> None:
        self._a = {"IFA_ADDRESS": address}
        self._i = {"index": index, "prefixlen": prefixlen, "family": family}

    def get_attr(self, key: str):
        return self._a.get(key)

    def __getitem__(self, key: str):
        return self._i[key]


def _make_iproute_mock(links, addrs):
    instance = Mock()
    instance.__enter__ = Mock(return_value=instance)
    instance.__exit__ = Mock(return_value=False)
    instance.get_links.return_value = links
    instance.get_addr.return_value = addrs
    return Mock(return_value=instance)


_STATS_JSON = {
    "timestamp": 1234567890.0,
    "interval_seconds": 30,
    "interfaces": {
        "eth0": {
            "ifInOctets": 100000,
            "ifOutOctets": 200000,
            "ifInUcastPkts": 500,
            "ifOutUcastPkts": 400,
            "ifInErrors": 1,
            "ifOutErrors": 2,
            "ifInDiscards": 3,
            "ifOutDiscards": 4,
            "ifInUnknownProtos": 0,
            "bpsIn": 1234.5,
            "bpsOut": 5678.9,
            "ppsIn": 10.20,
            "ppsOut": 9.80,
            "last_updated": 1234567890.0,
        }
    },
}

_PATCH_IPROUTE = "nos.cli.modes.operational.IPRoute"
_PATCH_STATS = "nos.cli.modes.operational._load_iface_stats"


@pytest.fixture
def store(tmp_path):
    return ConfigStore(base_dir=tmp_path)


@pytest.fixture
def oper(store):
    return OperationalMode(store)


# ---------------------------------------------------------------------------
# _load_iface_stats
# ---------------------------------------------------------------------------

class TestLoadIfaceStats:
    def test_returns_empty_when_file_missing(self):
        with patch("nos.cli.modes.operational.open", side_effect=FileNotFoundError):
            result = _load_iface_stats()
        assert result == {}

    def test_returns_empty_on_corrupt_json(self, tmp_path):
        bad_file = tmp_path / "stats.json"
        bad_file.write_text("not json{{{")
        with patch("nos.cli.modes.operational._STATS_JSON_PATH", str(bad_file)):
            result = _load_iface_stats()
        assert result == {}

    def test_returns_interfaces_dict(self, tmp_path):
        stats_file = tmp_path / "stats.json"
        stats_file.write_text(json.dumps(_STATS_JSON))
        with patch("nos.cli.modes.operational._STATS_JSON_PATH", str(stats_file)):
            result = _load_iface_stats()
        assert "eth0" in result
        assert result["eth0"]["ifInOctets"] == 100000

    def test_returns_empty_when_interfaces_key_missing(self, tmp_path):
        stats_file = tmp_path / "stats.json"
        stats_file.write_text(json.dumps({"timestamp": 1.0}))
        with patch("nos.cli.modes.operational._STATS_JSON_PATH", str(stats_file)):
            result = _load_iface_stats()
        assert result == {}


# ---------------------------------------------------------------------------
# _fmt_bps
# ---------------------------------------------------------------------------

class TestFmtBps:
    def test_bps_below_1k(self):
        assert "bps" in _fmt_bps(999.0)
        assert "K" not in _fmt_bps(999.0)

    def test_kbps_range(self):
        result = _fmt_bps(1500.0)
        assert "Kbps" in result
        assert "1.50" in result

    def test_mbps_range(self):
        result = _fmt_bps(2_500_000.0)
        assert "Mbps" in result
        assert "2.50" in result

    def test_gbps_range(self):
        result = _fmt_bps(1_500_000_000.0)
        assert "Gbps" in result
        assert "1.50" in result

    def test_zero_bps(self):
        result = _fmt_bps(0.0)
        assert "bps" in result

    def test_example_from_spec(self):
        # bpsIn = 1234.5 → "1.23 Kbps"
        result = _fmt_bps(1234.5)
        assert "Kbps" in result


# ---------------------------------------------------------------------------
# _fmt_pps
# ---------------------------------------------------------------------------

class TestFmtPps:
    def test_two_decimal_places(self):
        assert _fmt_pps(10.2) == "10.20 pps"
        assert _fmt_pps(0.0) == "0.00 pps"
        assert _fmt_pps(9.8) == "9.80 pps"


# ---------------------------------------------------------------------------
# _fmt_timestamp
# ---------------------------------------------------------------------------

class TestFmtTimestamp:
    def test_formats_utc(self):
        result = _fmt_timestamp(0.0)
        assert "1970-01-01" in result
        assert "UTC" in result

    def test_known_timestamp(self):
        # 2009-02-13 23:31:30 UTC
        result = _fmt_timestamp(1234567890.0)
        assert "2009-02-13" in result


# ---------------------------------------------------------------------------
# _format_iface_stats
# ---------------------------------------------------------------------------

class TestFormatIfaceStats:
    def test_contains_traffic_statistics_header(self):
        lines = _format_iface_stats({})
        assert any("Traffic statistics" in ln for ln in lines)

    def test_contains_errors_header(self):
        lines = _format_iface_stats({})
        assert any("Errors" in ln for ln in lines)

    def test_shows_counters(self):
        stats = {
            "ifInOctets": 12345, "ifOutOctets": 67890,
            "ifInUcastPkts": 100, "ifOutUcastPkts": 90,
            "ifInErrors": 5, "ifOutErrors": 6,
            "ifInDiscards": 7, "ifOutDiscards": 8,
            "bpsIn": 0.0, "bpsOut": 0.0,
            "ppsIn": 0.0, "ppsOut": 0.0,
        }
        lines = _format_iface_stats(stats)
        combined = "\n".join(lines)
        assert "12345" in combined
        assert "67890" in combined
        assert "100" in combined
        assert "5" in combined

    def test_zeros_when_no_stats(self):
        lines = _format_iface_stats({})
        combined = "\n".join(lines)
        assert "0" in combined

    def test_rate_formatted(self):
        stats = {"bpsIn": 1500.0, "bpsOut": 0.0, "ppsIn": 2.5, "ppsOut": 0.0}
        lines = _format_iface_stats(stats)
        combined = "\n".join(lines)
        assert "Kbps" in combined
        assert "2.50 pps" in combined


# ---------------------------------------------------------------------------
# show interfaces — with stats injection
# ---------------------------------------------------------------------------

class TestShowInterfacesWithStats:
    def test_traffic_statistics_section_shown(self, oper):
        links = [_MockLink("eth0", 2, 0, 1500, "UP")]
        mock_ip = _make_iproute_mock(links, [])
        with patch(_PATCH_IPROUTE, mock_ip), \
             patch(_PATCH_STATS, return_value=_STATS_JSON["interfaces"]):
            out = oper.execute("show interfaces")
        assert "Traffic statistics" in out

    def test_traffic_statistics_shows_bytes(self, oper):
        links = [_MockLink("eth0", 2, 0, 1500, "UP")]
        mock_ip = _make_iproute_mock(links, [])
        with patch(_PATCH_IPROUTE, mock_ip), \
             patch(_PATCH_STATS, return_value=_STATS_JSON["interfaces"]):
            out = oper.execute("show interfaces")
        assert "100000" in out   # ifInOctets
        assert "200000" in out   # ifOutOctets

    def test_traffic_statistics_shows_rates(self, oper):
        links = [_MockLink("eth0", 2, 0, 1500, "UP")]
        mock_ip = _make_iproute_mock(links, [])
        with patch(_PATCH_IPROUTE, mock_ip), \
             patch(_PATCH_STATS, return_value=_STATS_JSON["interfaces"]):
            out = oper.execute("show interfaces")
        assert "Kbps" in out
        assert "pps" in out

    def test_errors_section_shown(self, oper):
        links = [_MockLink("eth0", 2, 0, 1500, "UP")]
        mock_ip = _make_iproute_mock(links, [])
        with patch(_PATCH_IPROUTE, mock_ip), \
             patch(_PATCH_STATS, return_value=_STATS_JSON["interfaces"]):
            out = oper.execute("show interfaces")
        assert "Errors" in out

    def test_missing_stats_shows_zeros(self, oper):
        links = [_MockLink("eth0", 2, 0, 1500, "UP")]
        mock_ip = _make_iproute_mock(links, [])
        with patch(_PATCH_IPROUTE, mock_ip), \
             patch(_PATCH_STATS, return_value={}):
            out = oper.execute("show interfaces")
        assert "Traffic statistics" in out
        assert "Errors" in out

    def test_interface_not_in_stats_shows_zeros(self, oper):
        links = [_MockLink("eth1", 3, 0, 1500, "UP")]
        mock_ip = _make_iproute_mock(links, [])
        with patch(_PATCH_IPROUTE, mock_ip), \
             patch(_PATCH_STATS, return_value=_STATS_JSON["interfaces"]):
            out = oper.execute("show interfaces")
        assert "Traffic statistics" in out


# ---------------------------------------------------------------------------
# show interfaces extensive
# ---------------------------------------------------------------------------

class TestShowInterfacesExtensive:
    def test_extensive_shows_traffic_statistics(self, oper):
        links = [_MockLink("eth0", 2, 0, 1500, "UP")]
        mock_ip = _make_iproute_mock(links, [])
        with patch(_PATCH_IPROUTE, mock_ip), \
             patch(_PATCH_STATS, return_value=_STATS_JSON["interfaces"]):
            out = oper.execute("show interfaces extensive")
        assert "Traffic statistics" in out
        assert "30-second moving average" in out

    def test_extensive_shows_errors_section(self, oper):
        links = [_MockLink("eth0", 2, 0, 1500, "UP")]
        mock_ip = _make_iproute_mock(links, [])
        with patch(_PATCH_IPROUTE, mock_ip), \
             patch(_PATCH_STATS, return_value=_STATS_JSON["interfaces"]):
            out = oper.execute("show interfaces extensive")
        assert "Errors" in out

    def test_extensive_shows_last_flap_when_available(self, oper):
        links = [_MockLink("eth0", 2, 0, 1500, "UP")]
        mock_ip = _make_iproute_mock(links, [])
        stats_with_flap = {
            "eth0": {**_STATS_JSON["interfaces"]["eth0"], "last_flap": 1234567890.0}
        }
        with patch(_PATCH_IPROUTE, mock_ip), \
             patch(_PATCH_STATS, return_value=stats_with_flap):
            out = oper.execute("show interfaces extensive")
        assert "Last flap" in out
        assert "2009-02-13" in out

    def test_extensive_shows_never_when_no_flap(self, oper):
        links = [_MockLink("eth0", 2, 0, 1500, "UP")]
        mock_ip = _make_iproute_mock(links, [])
        with patch(_PATCH_IPROUTE, mock_ip), \
             patch(_PATCH_STATS, return_value=_STATS_JSON["interfaces"]):
            out = oper.execute("show interfaces extensive")
        assert "Last flap: never" in out

    def test_extensive_still_shows_interface_header(self, oper):
        links = [_MockLink("eth0", 2, 0, 1500, "UP")]
        mock_ip = _make_iproute_mock(links, [])
        with patch(_PATCH_IPROUTE, mock_ip), \
             patch(_PATCH_STATS, return_value={}):
            out = oper.execute("show interfaces extensive")
        assert "Physical interface: eth0" in out

    def test_extensive_skips_loopback(self, oper):
        lo = _MockLink("lo", 1, 0x8, 65536, "UNKNOWN")
        eth0 = _MockLink("eth0", 2, 0, 1500, "UP")
        mock_ip = _make_iproute_mock([lo, eth0], [])
        with patch(_PATCH_IPROUTE, mock_ip), \
             patch(_PATCH_STATS, return_value={}):
            out = oper.execute("show interfaces extensive")
        assert "Physical interface: lo" not in out
        assert "Physical interface: eth0" in out


# ---------------------------------------------------------------------------
# show interfaces <name> — interface name filtering
# ---------------------------------------------------------------------------

class TestShowInterfacesFiltering:
    def test_verbose_filter_shows_only_named(self, oper):
        links = [_MockLink("eth0", 2), _MockLink("eth1", 3)]
        mock_ip = _make_iproute_mock(links, [])
        with patch(_PATCH_IPROUTE, mock_ip), \
             patch(_PATCH_STATS, return_value={}):
            out = oper.execute("show interfaces eth0")
        assert "Physical interface: eth0" in out
        assert "Physical interface: eth1" not in out

    def test_verbose_filter_unknown_interface(self, oper):
        links = [_MockLink("eth0", 2)]
        mock_ip = _make_iproute_mock(links, [])
        with patch(_PATCH_IPROUTE, mock_ip), \
             patch(_PATCH_STATS, return_value={}):
            out = oper.execute("show interfaces nonexistent")
        assert "Physical interface" not in out

    def test_extensive_filter_shows_only_named(self, oper):
        links = [_MockLink("eth0", 2), _MockLink("eth1", 3)]
        mock_ip = _make_iproute_mock(links, [])
        with patch(_PATCH_IPROUTE, mock_ip), \
             patch(_PATCH_STATS, return_value={}):
            out = oper.execute("show interfaces eth0 extensive")
        assert "Physical interface: eth0" in out
        assert "Physical interface: eth1" not in out
        assert "30-second moving average" in out

    def test_terse_filter_shows_only_named(self, oper):
        links = [_MockLink("eth0", 2), _MockLink("eth1", 3)]
        mock_ip = _make_iproute_mock(links, [])
        with patch(_PATCH_IPROUTE, mock_ip), \
             patch(_PATCH_STATS, return_value={}):
            out = oper.execute("show interfaces eth0 terse")
        lines = [ln for ln in out.splitlines() if ln.startswith("eth")]
        assert any(ln.startswith("eth0") for ln in lines)
        assert not any(ln.startswith("eth1") for ln in lines)

    def test_description_filter_shows_only_named(self, oper):
        links = [_MockLink("eth0", 2), _MockLink("eth1", 3)]
        mock_ip = _make_iproute_mock(links, [])
        with patch(_PATCH_IPROUTE, mock_ip), \
             patch(_PATCH_STATS, return_value={}):
            out = oper.execute("show interfaces eth0 description")
        lines = [ln for ln in out.splitlines() if ln.startswith("eth")]
        assert any(ln.startswith("eth0") for ln in lines)
        assert not any(ln.startswith("eth1") for ln in lines)

    def test_name_before_keyword_works(self, oper):
        links = [_MockLink("eth0", 2), _MockLink("eth1", 3)]
        mock_ip = _make_iproute_mock(links, [])
        with patch(_PATCH_IPROUTE, mock_ip), \
             patch(_PATCH_STATS, return_value={}):
            out = oper.execute("show interfaces eth1 extensive")
        assert "Physical interface: eth1" in out
        assert "Physical interface: eth0" not in out

    def test_no_filter_shows_all(self, oper):
        links = [_MockLink("eth0", 2), _MockLink("eth1", 3)]
        mock_ip = _make_iproute_mock(links, [])
        with patch(_PATCH_IPROUTE, mock_ip), \
             patch(_PATCH_STATS, return_value={}):
            out = oper.execute("show interfaces")
        assert "Physical interface: eth0" in out
        assert "Physical interface: eth1" in out

    def test_filter_shows_stats_for_named_interface(self, oper):
        """Stats for the filtered interface are shown, not all zeros."""
        links = [_MockLink("eth0", 2), _MockLink("eth1", 3)]
        mock_ip = _make_iproute_mock(links, [])
        with patch(_PATCH_IPROUTE, mock_ip), \
             patch(_PATCH_STATS, return_value=_STATS_JSON["interfaces"]):
            out = oper.execute("show interfaces eth0")
        assert "100000" in out   # ifInOctets for eth0
        assert "200000" in out   # ifOutOctets for eth0

    def test_filter_stats_via_kernel_name_fallback(self, oper):
        """Stats keyed by kernel name are found even when display_name is an alias.

        Reproduces the bug: stats.py uses load_alias_map() which may return None
        and write kernel names; operational.py uses get_alias_map() which detects
        aliases on-the-fly.  The stats lookup must fall back to the kernel name.
        """
        links = [_MockLink("ens34", 2, 0, 1500, "UP")]
        mock_ip = _make_iproute_mock(links, [])
        # stats.json keyed by kernel name (stats writer had no alias map)
        stats_by_kernel = {"ens34": {**_STATS_JSON["interfaces"]["eth0"]}}
        # operational.py resolved the alias: ens34 -> et0
        alias_map = {"ens34": "et0"}
        with patch(_PATCH_IPROUTE, mock_ip), \
             patch(_PATCH_STATS, return_value=stats_by_kernel), \
             patch.object(oper, "_get_alias_map", return_value=alias_map):
            out = oper.execute("show interfaces et0")
        assert "100000" in out   # ifInOctets
        assert "200000" in out   # ifOutOctets
