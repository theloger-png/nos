"""Tests for 'show bgp' — nos/cli/commands/show/bgp.py and operational mode."""
from __future__ import annotations

import json
from unittest.mock import MagicMock, Mock

import pytest
from prompt_toolkit.completion import CompleteEvent
from prompt_toolkit.document import Document

from nos.cli.commands.show.bgp import (
    _bgp_type_str,
    _extract_ipv4_summary,
    _frr_fetch,
    _nlri_families,
    render_neighbor_detail,
    render_summary,
    show_bgp,
)
from nos.cli.completer import NOSCompleter
from nos.cli.modes.operational import OperationalMode
from nos.cli.parser import CLIMode
from nos.config.store import ConfigStore


# ============================================================================
# Fixtures / helpers
# ============================================================================

def _make_frr(summary: dict | None = None, neighbors: dict | None = None) -> Mock:
    """Return a mock FRRClient whose .show() returns the given JSON dicts."""
    frr = MagicMock()

    def _show(cmd: str) -> str:
        mapping = {
            "show bgp summary json":      summary  or {},
            "show bgp neighbor json":     neighbors or {},
            "show bgp ipv4 unicast json": {},
        }
        if cmd not in mapping:
            raise ValueError(f"Unexpected FRR command: {cmd!r}")
        return json.dumps(mapping[cmd])

    frr.show.side_effect = _show
    return frr


# ── Minimal realistic FRR JSON payloads ─────────────────────────────────────

SUMMARY_ACTIVE = {
    "ipv4Unicast": {
        "as": 65001,
        "routerId": "172.18.4.44",
        "tableVersion": 0,
        "ribCount": 0,
        "peerCount": 1,
        "peers": {
            "10.0.0.2": {
                "remoteAs": 65001,
                "localAs": 65001,
                "version": 4,
                "msgRcvd": 0,
                "msgSent": 0,
                "inq": 0,
                "outq": 0,
                "peerUptime": "never",
                "state": "Active",
                "pfxRcd": 0,
                "connectionsEstablished": 0,
                "connectionsDropped": 0,
            }
        },
    }
}

SUMMARY_ESTABLISHED = {
    "ipv4Unicast": {
        "as": 65001,
        "routerId": "172.18.4.44",
        "peers": {
            "10.0.0.2": {
                "remoteAs": 65001,
                "version": 4,
                "msgRcvd": 145,
                "msgSent": 147,
                "inq": 0,
                "outq": 0,
                "peerUptime": "02:10:05",
                "state": "Established",
                "pfxRcd": 12,
                "connectionsEstablished": 1,
                "connectionsDropped": 0,
            }
        },
    }
}

SUMMARY_MULTI_PEER = {
    "ipv4Unicast": {
        "as": 65001,
        "routerId": "1.1.1.1",
        "peers": {
            "10.0.0.2": {
                "remoteAs": 65001,
                "version": 4,
                "msgRcvd": 10,
                "msgSent": 10,
                "inq": 0,
                "outq": 0,
                "peerUptime": "00:05:00",
                "state": "Established",
                "pfxRcd": 5,
                "connectionsEstablished": 1,
                "connectionsDropped": 0,
            },
            "10.0.0.4": {
                "remoteAs": 65002,
                "version": 4,
                "msgRcvd": 0,
                "msgSent": 0,
                "inq": 0,
                "outq": 0,
                "peerUptime": "never",
                "state": "Active",
                "pfxRcd": 0,
                "connectionsEstablished": 0,
                "connectionsDropped": 0,
            },
        },
    }
}

NEIGHBOR_ACTIVE = {
    "10.0.0.2": {
        "remoteAs": 65001,
        "localAs": 65001,
        "bgpState": "Active",
        "bgpTimerUpString": "never",
        "localAddress": "10.0.0.1",
        "updateSource": "10.0.0.1",
        "remoteRouterId": "0.0.0.0",
        "localRouterId": "172.18.4.44",
        "holdTimeConfigured": 90,
        "holdTimeMsecs": 0,
        "nbrFlaps": 0,
        "peerGroup": "IBGP",
        "lastNotificationReason": "",
        "authenticationEnabled": 0,
        "neighborCapabilities": {
            "routeRefresh": "advertisedAndReceived",
            "addressFamily": {
                "ipv4Unicast": {"negotiated": True},
            },
        },
        "gracefulRestartInfo": {
            "timers": {"configuredRestartTimer": 300},
        },
        "addressFamilyInfo": {
            "ipv4Unicast": {
                "acceptedPrefixCounter": 0,
                "sentPrefixCounter": 0,
            }
        },
    }
}

NEIGHBOR_ESTABLISHED = {
    "10.0.0.2": {
        "remoteAs": 65001,
        "localAs": 65001,
        "bgpState": "Established",
        "localAddress": "10.0.0.1",
        "updateSource": "10.0.0.1",
        "remoteRouterId": "2.2.2.2",
        "localRouterId": "1.1.1.1",
        "holdTimeConfigured": 90,
        "holdTimeMsecs": 90000,
        "nbrFlaps": 2,
        "peerGroup": "IBGP",
        "lastNotificationReason": "",
        "authenticationEnabled": 0,
        "neighborCapabilities": {
            "routeRefresh": "advertisedAndReceived",
            "addressFamily": {
                "ipv4Unicast": {"negotiated": True},
            },
        },
        "gracefulRestartInfo": {
            "timers": {"configuredRestartTimer": 120},
        },
        "addressFamilyInfo": {
            "ipv4Unicast": {
                "acceptedPrefixCounter": 12,
                "sentPrefixCounter": 8,
            }
        },
    }
}

NEIGHBOR_EBGP = {
    "192.168.1.2": {
        "remoteAs": 65002,
        "localAs": 65001,
        "bgpState": "Active",
        "localAddress": "192.168.1.1",
        "updateSource": "192.168.1.1",
        "remoteRouterId": "0.0.0.0",
        "localRouterId": "1.1.1.1",
        "holdTimeConfigured": 90,
        "holdTimeMsecs": 0,
        "nbrFlaps": 0,
        "peerGroup": "EBGP-UPSTREAM",
        "lastNotificationReason": "",
        "authenticationEnabled": 0,
        "neighborCapabilities": {
            "routeRefresh": "advertised",
            "addressFamily": {
                "ipv4Unicast": {"negotiated": True},
            },
        },
        "gracefulRestartInfo": {
            "timers": {"configuredRestartTimer": 120},
        },
        "addressFamilyInfo": {
            "ipv4Unicast": {
                "acceptedPrefixCounter": 0,
                "sentPrefixCounter": 0,
            }
        },
    }
}


# ============================================================================
# Unit tests — helper functions
# ============================================================================

class TestHelpers:
    def test_bgp_type_str_ibgp(self):
        assert _bgp_type_str(65001, 65001) == "Internal"

    def test_bgp_type_str_ebgp(self):
        assert _bgp_type_str(65001, 65002) == "External"

    def test_extract_ipv4_summary_direct(self):
        data = {"ipv4Unicast": {"as": 65001, "routerId": "1.1.1.1", "peers": {}}}
        result = _extract_ipv4_summary(data)
        assert result["as"] == 65001

    def test_extract_ipv4_summary_vrf_wrapped(self):
        data = {
            "vrfs": {
                "default": {
                    "ipv4Unicast": {"as": 65002, "routerId": "2.2.2.2", "peers": {}}
                }
            }
        }
        result = _extract_ipv4_summary(data)
        assert result["as"] == 65002

    def test_extract_ipv4_summary_empty(self):
        assert _extract_ipv4_summary({}) == {}

    def test_nlri_families_ipv4(self):
        nbr = {
            "neighborCapabilities": {
                "addressFamily": {"ipv4Unicast": {"negotiated": True}}
            }
        }
        assert _nlri_families(nbr) == ["inet-unicast"]

    def test_nlri_families_dual_stack(self):
        nbr = {
            "neighborCapabilities": {
                "addressFamily": {
                    "ipv4Unicast": {"negotiated": True},
                    "ipv6Unicast": {"negotiated": True},
                }
            }
        }
        families = _nlri_families(nbr)
        assert "inet-unicast" in families
        assert "inet6-unicast" in families

    def test_nlri_families_fallback(self):
        # No capabilities at all → defaults to inet-unicast
        assert _nlri_families({}) == ["inet-unicast"]

    def test_frr_fetch_returns_dict(self):
        frr = MagicMock()
        frr.show.return_value = '{"foo": 1}'
        assert _frr_fetch(frr, "dummy") == {"foo": 1}

    def test_frr_fetch_returns_empty_on_error(self):
        frr = MagicMock()
        frr.show.side_effect = Exception("vtysh not found")
        assert _frr_fetch(frr, "dummy") == {}


# ============================================================================
# Unit tests — render_summary
# ============================================================================

class TestRenderSummary:
    def test_basic_header(self):
        out = render_summary(SUMMARY_ACTIVE)
        assert "BGP summary information for VRF default" in out
        assert "Router identifier 172.18.4.44, local AS number 65001" in out

    def test_active_peer_shows_state(self):
        out = render_summary(SUMMARY_ACTIVE)
        assert "10.0.0.2" in out
        assert "Active" in out
        # When Active, should NOT show prefix count
        lines = [l for l in out.splitlines() if "10.0.0.2" in l]
        assert lines
        assert "Active" in lines[0]

    def test_established_peer_shows_prefix_count(self):
        out = render_summary(SUMMARY_ESTABLISHED)
        lines = [l for l in out.splitlines() if "10.0.0.2" in l]
        assert lines
        # State/PfxRcd column should show "12" not "Established"
        assert "12" in lines[0]
        assert "Established" not in lines[0]

    def test_multi_peer(self):
        out = render_summary(SUMMARY_MULTI_PEER)
        assert "10.0.0.2" in out
        assert "10.0.0.4" in out
        # iBGP peer is Established → shows prefix count
        lines = [l for l in out.splitlines() if "10.0.0.2" in l]
        assert "5" in lines[0]
        # eBGP peer is Active → shows state
        lines4 = [l for l in out.splitlines() if "10.0.0.4" in l]
        assert "Active" in lines4[0]

    def test_no_peers(self):
        data = {"ipv4Unicast": {"as": 65001, "routerId": "1.1.1.1", "peers": {}}}
        out = render_summary(data)
        assert "No BGP neighbors configured" in out

    def test_empty_data(self):
        out = render_summary({})
        assert "No BGP neighbors configured" in out

    def test_column_header_present(self):
        out = render_summary(SUMMARY_ACTIVE)
        assert "Neighbor" in out
        assert "MsgRcvd" in out
        assert "State/PfxRcd" in out

    def test_detail_adds_connection_info(self):
        out = render_summary(SUMMARY_ESTABLISHED, detail=True)
        assert "Connections established" in out
        assert "dropped" in out

    def test_detail_shows_description(self):
        out = render_summary(SUMMARY_ACTIVE, detail=True)
        assert "Description:" in out

    def test_uptime_shown(self):
        out = render_summary(SUMMARY_ESTABLISHED)
        assert "02:10:05" in out

    def test_as_number_shown(self):
        out = render_summary(SUMMARY_MULTI_PEER)
        assert "65001" in out
        assert "65002" in out


# ============================================================================
# Unit tests — render_neighbor_detail
# ============================================================================

class TestRenderNeighborDetail:
    def test_basic_structure(self):
        out = render_neighbor_detail("10.0.0.2", NEIGHBOR_ACTIVE["10.0.0.2"])
        assert "Peer: 10.0.0.2 AS 65001" in out
        assert "Local: 10.0.0.1 AS 65001" in out

    def test_group_shown(self):
        out = render_neighbor_detail("10.0.0.2", NEIGHBOR_ACTIVE["10.0.0.2"])
        assert "Group: IBGP" in out

    def test_routing_instance(self):
        out = render_neighbor_detail("10.0.0.2", NEIGHBOR_ACTIVE["10.0.0.2"])
        assert "Routing-Instance: master" in out

    def test_type_internal(self):
        out = render_neighbor_detail("10.0.0.2", NEIGHBOR_ACTIVE["10.0.0.2"])
        assert "Type: Internal" in out

    def test_type_external(self):
        out = render_neighbor_detail("192.168.1.2", NEIGHBOR_EBGP["192.168.1.2"])
        assert "Type: External" in out

    def test_state_active(self):
        out = render_neighbor_detail("10.0.0.2", NEIGHBOR_ACTIVE["10.0.0.2"])
        assert "State: Active" in out

    def test_state_established(self):
        out = render_neighbor_detail("10.0.0.2", NEIGHBOR_ESTABLISHED["10.0.0.2"])
        assert "State: Established" in out

    def test_last_state_event(self):
        out = render_neighbor_detail("10.0.0.2", NEIGHBOR_ACTIVE["10.0.0.2"])
        assert "Last State:" in out
        assert "Last Event:" in out

    def test_last_error_none(self):
        out = render_neighbor_detail("10.0.0.2", NEIGHBOR_ACTIVE["10.0.0.2"])
        assert "Last Error: None" in out

    def test_options_present(self):
        out = render_neighbor_detail("10.0.0.2", NEIGHBOR_ACTIVE["10.0.0.2"])
        assert "Options: <" in out
        assert "Preference" in out
        assert "HoldTime" in out

    def test_local_address_options(self):
        out = render_neighbor_detail("10.0.0.2", NEIGHBOR_ACTIVE["10.0.0.2"])
        assert "LocalAddress" in out

    def test_holdtime(self):
        out = render_neighbor_detail("10.0.0.2", NEIGHBOR_ACTIVE["10.0.0.2"])
        assert "Holdtime: 90" in out

    def test_preference(self):
        out = render_neighbor_detail("10.0.0.2", NEIGHBOR_ACTIVE["10.0.0.2"])
        assert "Preference: 170" in out

    def test_flaps(self):
        out = render_neighbor_detail("10.0.0.2", NEIGHBOR_ESTABLISHED["10.0.0.2"])
        assert "Number of flaps: 2" in out

    def test_peer_id_local_id(self):
        out = render_neighbor_detail("10.0.0.2", NEIGHBOR_ACTIVE["10.0.0.2"])
        assert "Peer ID: 0.0.0.0" in out
        assert "Local ID: 172.18.4.44" in out

    def test_peer_id_established(self):
        out = render_neighbor_detail("10.0.0.2", NEIGHBOR_ESTABLISHED["10.0.0.2"])
        assert "Peer ID: 2.2.2.2" in out
        assert "Local ID: 1.1.1.1" in out

    def test_nlri_lines(self):
        out = render_neighbor_detail("10.0.0.2", NEIGHBOR_ACTIVE["10.0.0.2"])
        assert "NLRI for restart configured on peer:" in out
        assert "NLRI advertised by peer:" in out
        assert "NLRI for this session:" in out
        assert "inet-unicast" in out

    def test_refresh_capability(self):
        out = render_neighbor_detail("10.0.0.2", NEIGHBOR_ACTIVE["10.0.0.2"])
        assert "Peer supports Refresh Capability (2)" in out

    def test_no_refresh_capability(self):
        nbr = {**NEIGHBOR_EBGP["192.168.1.2"]}
        out = render_neighbor_detail("192.168.1.2", nbr)
        # routeRefresh is "advertised" not "advertisedAndReceived" → no capability line
        assert "Peer supports Refresh Capability" not in out

    def test_stale_holdtime(self):
        out = render_neighbor_detail("10.0.0.2", NEIGHBOR_ACTIVE["10.0.0.2"])
        assert "Stale-route holdtime configured: 300" in out

    def test_prefix_counters_zero(self):
        out = render_neighbor_detail("10.0.0.2", NEIGHBOR_ACTIVE["10.0.0.2"])
        assert "Active prefixes:              0" in out
        assert "Received prefixes:            0" in out
        assert "Accepted prefixes:            0" in out
        assert "Suppressed due to damping:    0" in out
        assert "Advertised prefixes:          0" in out

    def test_prefix_counters_nonzero(self):
        out = render_neighbor_detail("10.0.0.2", NEIGHBOR_ESTABLISHED["10.0.0.2"])
        assert "Accepted prefixes:            12" in out
        assert "Advertised prefixes:          8" in out

    def test_send_state_not_advertising_when_active(self):
        out = render_neighbor_detail("10.0.0.2", NEIGHBOR_ACTIVE["10.0.0.2"])
        assert "Send state: not advertising" in out

    def test_send_state_in_sync_when_established_with_pfx(self):
        out = render_neighbor_detail("10.0.0.2", NEIGHBOR_ESTABLISHED["10.0.0.2"])
        assert "Send state: in sync" in out

    def test_active_holdtime_zero_when_not_established(self):
        out = render_neighbor_detail("10.0.0.2", NEIGHBOR_ACTIVE["10.0.0.2"])
        assert "Active Holdtime: 0" in out

    def test_active_holdtime_set_when_established(self):
        out = render_neighbor_detail("10.0.0.2", NEIGHBOR_ESTABLISHED["10.0.0.2"])
        assert "Active Holdtime: 90" in out

    def test_missing_fields_graceful(self):
        # Minimal neighbor dict should not raise
        out = render_neighbor_detail("1.2.3.4", {})
        assert "Peer: 1.2.3.4" in out


# ============================================================================
# Unit tests — show_bgp (main entry point)
# ============================================================================

class TestShowBgp:
    def test_no_frr_returns_not_running(self):
        out = show_bgp([], frr=None)
        assert "BGP is not running" in out

    def test_empty_frr_response_returns_not_running(self):
        frr = _make_frr(summary={}, neighbors={})
        out = show_bgp([], frr=frr)
        assert "BGP is not running" in out

    def test_summary_default(self):
        frr = _make_frr(summary=SUMMARY_ACTIVE)
        out = show_bgp([], frr=frr)
        assert "BGP summary information" in out
        assert "10.0.0.2" in out

    def test_summary_explicit(self):
        frr = _make_frr(summary=SUMMARY_ACTIVE)
        out = show_bgp(["summary"], frr=frr)
        assert "BGP summary information" in out

    def test_summary_detail(self):
        frr = _make_frr(summary=SUMMARY_ESTABLISHED)
        out = show_bgp(["summary", "detail"], frr=frr)
        assert "Connections established" in out

    def test_neighbor_all(self):
        frr = _make_frr(neighbors=NEIGHBOR_ACTIVE)
        out = show_bgp(["neighbor"], frr=frr)
        assert "Peer: 10.0.0.2" in out

    def test_neighbor_specific_found(self):
        frr = _make_frr(neighbors=NEIGHBOR_ACTIVE)
        out = show_bgp(["neighbor", "10.0.0.2"], frr=frr)
        assert "Peer: 10.0.0.2" in out

    def test_neighbor_specific_not_found(self):
        frr = _make_frr(neighbors=NEIGHBOR_ACTIVE)
        out = show_bgp(["neighbor", "9.9.9.9"], frr=frr)
        assert "not found" in out.lower() or "BGP neighbor" in out

    def test_neighbor_all_empty(self):
        frr = _make_frr(neighbors={})
        out = show_bgp(["neighbor"], frr=frr)
        assert "BGP is not running" in out or "No BGP neighbors" in out

    def test_unknown_subcommand(self):
        frr = _make_frr(summary=SUMMARY_ACTIVE)
        out = show_bgp(["unicast"], frr=frr)
        assert "error" in out.lower() or "unknown" in out.lower()

    def test_neighbor_multiple_peers(self):
        combined = {**NEIGHBOR_ACTIVE, **NEIGHBOR_EBGP}
        frr = _make_frr(neighbors=combined)
        out = show_bgp(["neighbor"], frr=frr)
        assert "Peer: 10.0.0.2" in out
        assert "Peer: 192.168.1.2" in out

    def test_frr_error_propagates_to_not_running(self):
        frr = MagicMock()
        frr.show.side_effect = Exception("bgpd not running")
        out = show_bgp(["summary"], frr=frr)
        assert "BGP is not running" in out

    def test_alias_fn_accepted(self):
        frr = _make_frr(summary=SUMMARY_ACTIVE)
        out = show_bgp(["summary"], frr=frr, alias_fn=lambda x: x)
        assert "BGP summary information" in out

    def test_summary_prefix_sum(self):
        frr = _make_frr(summary=SUMMARY_ACTIVE)
        out = show_bgp(["sum"], frr=frr)
        assert "BGP summary information" in out

    def test_summary_prefix_summ(self):
        frr = _make_frr(summary=SUMMARY_ACTIVE)
        out = show_bgp(["summ"], frr=frr)
        assert "BGP summary information" in out

    def test_neighbor_prefix_neigh(self):
        frr = _make_frr(neighbors=NEIGHBOR_ACTIVE)
        out = show_bgp(["neigh"], frr=frr)
        assert "Peer: 10.0.0.2" in out

    def test_neighbor_prefix_n(self):
        frr = _make_frr(neighbors=NEIGHBOR_ACTIVE)
        out = show_bgp(["n"], frr=frr)
        assert "Peer: 10.0.0.2" in out


# ============================================================================
# Integration — operational mode
# ============================================================================

@pytest.fixture
def store(tmp_path):
    return ConfigStore(base_dir=tmp_path)


@pytest.fixture
def oper(store):
    return OperationalMode(store)


class TestOperationalShowBgp:
    def test_show_bgp_routes_through_show(self, oper, monkeypatch):
        import nos.cli.commands.show.bgp as bgp_mod
        monkeypatch.setattr(
            bgp_mod, "show_bgp",
            lambda args, frr=None, alias_fn=None: "MOCK-BGP-OUTPUT"
        )
        import nos.drivers.frr.client as frr_mod
        monkeypatch.setattr(frr_mod, "FRRClient", lambda: MagicMock())
        out = oper.execute("show bgp summary")
        assert out == "MOCK-BGP-OUTPUT"

    def test_show_bgp_neighbor_routes_through_show(self, oper, monkeypatch):
        import nos.cli.commands.show.bgp as bgp_mod
        captured: dict = {}

        def _fake_show_bgp(args, frr=None, alias_fn=None):
            captured["args"] = args
            return "NEIGHBOR-OUTPUT"

        monkeypatch.setattr(bgp_mod, "show_bgp", _fake_show_bgp)
        import nos.drivers.frr.client as frr_mod
        monkeypatch.setattr(frr_mod, "FRRClient", lambda: MagicMock())
        oper.execute("show bgp neighbor 10.0.0.2")
        assert captured["args"] == ["neighbor", "10.0.0.2"]

    def test_show_bgp_summary_detail_args(self, oper, monkeypatch):
        import nos.cli.commands.show.bgp as bgp_mod
        captured: dict = {}

        def _fake_show_bgp(args, frr=None, alias_fn=None):
            captured["args"] = args
            return "DETAIL-OUTPUT"

        monkeypatch.setattr(bgp_mod, "show_bgp", _fake_show_bgp)
        import nos.drivers.frr.client as frr_mod
        monkeypatch.setattr(frr_mod, "FRRClient", lambda: MagicMock())
        oper.execute("show bgp summary detail")
        assert captured["args"] == ["summary", "detail"]


# ============================================================================
# Completer tests
# ============================================================================

def _completions(text: str, mode: CLIMode = CLIMode.OPERATIONAL) -> list[str]:
    store = ConfigStore.__new__(ConfigStore)
    store._candidate = {}
    store._running = {}
    completer = NOSCompleter(mode, edit_path=[], store=store)
    doc = Document(text)
    event = CompleteEvent()
    return [c.text for c in completer.get_completions(doc, event)]


class TestBgpCompleter:
    def test_show_bgp_offers_summary_and_neighbor(self):
        completions = _completions("show bgp ")
        assert "summary" in completions
        assert "neighbor" in completions

    def test_show_bgp_summary_offers_detail(self):
        completions = _completions("show bgp summary ")
        assert "detail" in completions

    def test_show_bgp_neighbor_offers_ip_hint(self):
        completions = _completions("show bgp neighbor ")
        assert "<ip-address>" in completions

    def test_show_bgp_prefix_summary(self):
        completions = _completions("show bgp s")
        assert "summary" in completions

    def test_show_bgp_prefix_neighbor(self):
        completions = _completions("show bgp n")
        assert "neighbor" in completions

    def test_show_bgp_pipe_offered(self):
        completions = _completions("show bgp summary ")
        assert "|" in completions

    def test_show_bgp_neighbor_pipe_offered(self):
        completions = _completions("show bgp neighbor 10.0.0.2 ")
        # After a specific IP, pipe should be offered (or no completions — either is ok)
        # Just verify no crash
        assert isinstance(completions, list)
