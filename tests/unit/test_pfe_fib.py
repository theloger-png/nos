"""Unit tests for nos.pfe.fib.FIBManager."""
import json
from unittest.mock import MagicMock, call

import pytest

from nos.pfe.fib import FIBError, FIBManager
from nos.pfe.ipc import PFEError


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _ok() -> dict:
    return {"status": "ok"}


def _err(message: str = "something went wrong") -> dict:
    return {"status": "error", "message": message}


def _manager(reply: dict | None = None) -> tuple[FIBManager, MagicMock]:
    """Return a (FIBManager, mock_client) pair.  mock_client.send_message returns reply."""
    client = MagicMock()
    client.send_message.return_value = reply or _ok()
    return FIBManager(client), client


# ---------------------------------------------------------------------------
# route_add
# ---------------------------------------------------------------------------

class TestRouteAdd:
    def test_sends_correct_message_with_nexthop(self):
        mgr, client = _manager()
        mgr.route_add("10.0.0.0/24", "192.168.1.1", 3)
        client.send_message.assert_called_once_with(
            {"type": "fib_add", "prefix": "10.0.0.0/24", "nexthop": "192.168.1.1", "ifindex": 3}
        )

    def test_sends_correct_message_without_nexthop(self):
        mgr, client = _manager()
        mgr.route_add("10.0.0.0/24", None, 3)
        sent = client.send_message.call_args[0][0]
        assert "nexthop" not in sent
        assert sent["prefix"] == "10.0.0.0/24"
        assert sent["ifindex"] == 3

    def test_ipv6_prefix(self):
        mgr, client = _manager()
        mgr.route_add("2001:db8::/32", "fe80::1", 2)
        sent = client.send_message.call_args[0][0]
        assert sent["prefix"] == "2001:db8::/32"

    def test_host_prefix(self):
        mgr, client = _manager()
        mgr.route_add("192.168.0.1/32", None, 1)
        sent = client.send_message.call_args[0][0]
        assert sent["prefix"] == "192.168.0.1/32"

    def test_invalid_prefix_raises_fib_error(self):
        mgr, _ = _manager()
        with pytest.raises(FIBError, match="invalid prefix"):
            mgr.route_add("not-a-prefix", None, 1)

    def test_invalid_nexthop_raises_fib_error(self):
        mgr, _ = _manager()
        with pytest.raises(FIBError, match="invalid nexthop"):
            mgr.route_add("10.0.0.0/8", "not-an-ip", 1)

    def test_error_reply_raises_fib_error(self):
        mgr, _ = _manager(_err("fib_add: failed to insert route"))
        with pytest.raises(FIBError, match="fib_add"):
            mgr.route_add("10.0.0.0/8", None, 1)

    def test_pfe_error_wrapped_as_fib_error(self):
        client = MagicMock()
        client.send_message.side_effect = PFEError("connection lost")
        mgr = FIBManager(client)
        with pytest.raises(FIBError, match="PFE communication error"):
            mgr.route_add("10.0.0.0/8", None, 1)

    def test_non_strict_prefix_accepted(self):
        """Host bits set — ipaddress accepts with strict=False."""
        mgr, client = _manager()
        mgr.route_add("10.0.0.1/24", None, 1)
        client.send_message.assert_called_once()

    def test_default_route_accepted(self):
        mgr, client = _manager()
        mgr.route_add("0.0.0.0/0", "192.168.0.1", 1)
        client.send_message.assert_called_once()


# ---------------------------------------------------------------------------
# route_del
# ---------------------------------------------------------------------------

class TestRouteDel:
    def test_sends_correct_message(self):
        mgr, client = _manager()
        mgr.route_del("10.0.0.0/24")
        client.send_message.assert_called_once_with(
            {"type": "fib_del", "prefix": "10.0.0.0/24"}
        )

    def test_invalid_prefix_raises_fib_error(self):
        mgr, _ = _manager()
        with pytest.raises(FIBError, match="invalid prefix"):
            mgr.route_del("bad/prefix")

    def test_error_reply_raises_fib_error(self):
        mgr, _ = _manager(_err("fib_del: prefix not found"))
        with pytest.raises(FIBError, match="fib_del"):
            mgr.route_del("10.0.0.0/8")


# ---------------------------------------------------------------------------
# neigh_add
# ---------------------------------------------------------------------------

class TestNeighAdd:
    def test_sends_correct_message(self):
        mgr, client = _manager()
        mgr.neigh_add("192.168.1.1", "aa:bb:cc:dd:ee:ff", 2)
        client.send_message.assert_called_once_with(
            {"type": "neigh_add", "ip": "192.168.1.1", "mac": "aa:bb:cc:dd:ee:ff", "ifindex": 2}
        )

    def test_ipv6_neighbor(self):
        mgr, client = _manager()
        mgr.neigh_add("fe80::1", "11:22:33:44:55:66", 1)
        sent = client.send_message.call_args[0][0]
        assert sent["ip"] == "fe80::1"

    def test_uppercase_mac_accepted(self):
        mgr, client = _manager()
        mgr.neigh_add("10.0.0.1", "AA:BB:CC:DD:EE:FF", 1)
        client.send_message.assert_called_once()

    def test_invalid_ip_raises_fib_error(self):
        mgr, _ = _manager()
        with pytest.raises(FIBError, match="invalid ip"):
            mgr.neigh_add("not-an-ip", "aa:bb:cc:dd:ee:ff", 1)

    @pytest.mark.parametrize("bad_mac", [
        "aa:bb:cc:dd:ee",          # too short
        "aa:bb:cc:dd:ee:ff:00",    # too long
        "aa-bb-cc-dd-ee-ff",       # wrong separator
        "gg:bb:cc:dd:ee:ff",       # invalid hex
        "aabbccddeeff",            # no separators
        "",                         # empty
    ])
    def test_invalid_mac_raises_fib_error(self, bad_mac: str):
        mgr, _ = _manager()
        with pytest.raises(FIBError, match="invalid MAC"):
            mgr.neigh_add("10.0.0.1", bad_mac, 1)

    def test_error_reply_raises_fib_error(self):
        mgr, _ = _manager(_err("neigh_add: failed to insert neighbor"))
        with pytest.raises(FIBError, match="neigh_add"):
            mgr.neigh_add("10.0.0.1", "aa:bb:cc:dd:ee:ff", 1)


# ---------------------------------------------------------------------------
# neigh_del
# ---------------------------------------------------------------------------

class TestNeighDel:
    def test_sends_correct_message(self):
        mgr, client = _manager()
        mgr.neigh_del("192.168.1.1")
        client.send_message.assert_called_once_with(
            {"type": "neigh_del", "ip": "192.168.1.1"}
        )

    def test_invalid_ip_raises_fib_error(self):
        mgr, _ = _manager()
        with pytest.raises(FIBError, match="invalid ip"):
            mgr.neigh_del("bad-ip")

    def test_error_reply_raises_fib_error(self):
        mgr, _ = _manager(_err("neigh_del: entry not found"))
        with pytest.raises(FIBError, match="neigh_del"):
            mgr.neigh_del("10.0.0.1")


# ---------------------------------------------------------------------------
# vlan_set
# ---------------------------------------------------------------------------

class TestVlanSet:
    def test_sends_correct_message(self):
        mgr, client = _manager()
        mgr.vlan_set(100, 4)
        client.send_message.assert_called_once_with(
            {"type": "vlan_set", "vlan_id": 100, "ifindex": 4}
        )

    def test_boundary_vlan_1(self):
        mgr, client = _manager()
        mgr.vlan_set(1, 1)
        client.send_message.assert_called_once()

    def test_boundary_vlan_4094(self):
        mgr, client = _manager()
        mgr.vlan_set(4094, 1)
        client.send_message.assert_called_once()

    @pytest.mark.parametrize("bad_vlan", [0, 4095, -1, 9999])
    def test_out_of_range_vlan_raises_fib_error(self, bad_vlan: int):
        mgr, _ = _manager()
        with pytest.raises(FIBError, match="invalid VLAN ID"):
            mgr.vlan_set(bad_vlan, 1)

    def test_error_reply_raises_fib_error(self):
        mgr, _ = _manager(_err("vlan_set: failed to set VLAN mapping"))
        with pytest.raises(FIBError, match="vlan_set"):
            mgr.vlan_set(100, 1)


# ---------------------------------------------------------------------------
# vlan_del
# ---------------------------------------------------------------------------

class TestVlanDel:
    def test_sends_correct_message(self):
        mgr, client = _manager()
        mgr.vlan_del(200)
        client.send_message.assert_called_once_with(
            {"type": "vlan_del", "vlan_id": 200}
        )

    @pytest.mark.parametrize("bad_vlan", [0, 4095])
    def test_out_of_range_vlan_raises_fib_error(self, bad_vlan: int):
        mgr, _ = _manager()
        with pytest.raises(FIBError, match="invalid VLAN ID"):
            mgr.vlan_del(bad_vlan)

    def test_error_reply_raises_fib_error(self):
        mgr, _ = _manager(_err("vlan_del: entry not found"))
        with pytest.raises(FIBError, match="vlan_del"):
            mgr.vlan_del(200)

    def test_pfe_error_wrapped_as_fib_error(self):
        client = MagicMock()
        client.send_message.side_effect = PFEError("socket dead")
        mgr = FIBManager(client)
        with pytest.raises(FIBError, match="PFE communication error"):
            mgr.vlan_del(10)


# ---------------------------------------------------------------------------
# _send: edge cases on the reply envelope
# ---------------------------------------------------------------------------

class TestReplyHandling:
    def test_missing_status_field_raises_fib_error(self):
        mgr, _ = _manager({"result": "whatever"})
        with pytest.raises(FIBError):
            mgr.route_del("10.0.0.0/8")

    def test_error_reply_without_message_field(self):
        mgr, _ = _manager({"status": "error"})
        with pytest.raises(FIBError, match="unknown error"):
            mgr.route_del("10.0.0.0/8")
