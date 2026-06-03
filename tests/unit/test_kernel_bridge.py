"""Unit tests for nos.drivers.kernel.bridge.BridgeDriver."""
from unittest.mock import MagicMock, call

import pytest

from nos.drivers.kernel.bridge import (
    BridgeDriver,
    _BRIDGE_VLAN_INFO_PVID,
    _BRIDGE_VLAN_INFO_UNTAGGED,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_driver(mock_ip, bridge_name="nos-br"):
    factory = MagicMock()
    factory.return_value.__enter__ = MagicMock(return_value=mock_ip)
    factory.return_value.__exit__ = MagicMock(return_value=False)
    return BridgeDriver(bridge_name=bridge_name, iproute_factory=factory)


def _link_get_response(master_idx=0):
    msg = MagicMock()
    msg.get_attr.side_effect = lambda k: master_idx if k == "IFLA_MASTER" else None
    return [msg]


# ---------------------------------------------------------------------------
# apply_bridge — creation
# ---------------------------------------------------------------------------

def test_apply_bridge_creates_bridge_if_absent():
    ip = MagicMock()
    # First lookup: not found; second (after creation): found at index 10.
    ip.link_lookup.side_effect = [[], [10]]
    ip.link.return_value = _link_get_response()

    driver = _make_driver(ip)
    driver.apply_bridge("nos-br", {})

    ip.link.assert_any_call(
        "add", ifname="nos-br", kind="bridge", br_vlan_filtering=1
    )


def test_apply_bridge_skips_creation_if_exists():
    ip = MagicMock()
    ip.link_lookup.return_value = [10]
    ip.link.return_value = _link_get_response()

    driver = _make_driver(ip)
    driver.apply_bridge("nos-br", {})

    for c in ip.link.call_args_list:
        assert c.args[0] != "add", "Should not recreate an existing bridge"


def test_apply_bridge_brings_up():
    ip = MagicMock()
    ip.link_lookup.return_value = [10]
    ip.link.return_value = _link_get_response()

    driver = _make_driver(ip)
    driver.apply_bridge("nos-br", {})

    ip.link.assert_any_call("set", index=10, state="up")


def test_apply_bridge_adds_ports():
    ip = MagicMock()
    # bridge: idx 10; eth1: idx 3; eth2: idx 4
    def _lookup(ifname):
        return {"nos-br": [10], "eth1": [3], "eth2": [4]}.get(ifname, [])
    ip.link_lookup.side_effect = lambda ifname: _lookup(ifname)
    ip.link.return_value = _link_get_response(master_idx=0)

    driver = _make_driver(ip)
    driver.apply_bridge("nos-br", {"ports": ["eth1", "eth2"]})

    ip.link.assert_any_call("set", index=3, master=10, state="up")
    ip.link.assert_any_call("set", index=4, master=10, state="up")


def test_apply_bridge_skips_missing_port(caplog):
    ip = MagicMock()
    def _lookup(ifname):
        return [10] if ifname == "nos-br" else []
    ip.link_lookup.side_effect = lambda ifname: _lookup(ifname)
    ip.link.return_value = _link_get_response()

    driver = _make_driver(ip)
    with caplog.at_level("WARNING"):
        driver.apply_bridge("nos-br", {"ports": ["ghost0"]})

    assert "ghost0" in caplog.text


# ---------------------------------------------------------------------------
# apply_vlan — access mode
# ---------------------------------------------------------------------------

def test_apply_vlan_access_sets_pvid_untagged():
    ip = MagicMock()
    def _lookup(ifname):
        return {"nos-br": [10], "eth2": [4]}.get(ifname, [])
    ip.link_lookup.side_effect = lambda ifname: _lookup(ifname)
    ip.link.return_value = _link_get_response(master_idx=10)
    ip.vlan_filter.return_value = None

    driver = _make_driver(ip)
    driver.apply_vlan("nos-br", "eth2", {"interface_mode": "access", "vlans": [100]})

    expected_flags = _BRIDGE_VLAN_INFO_PVID | _BRIDGE_VLAN_INFO_UNTAGGED
    ip.vlan_filter.assert_any_call(
        "add", index=4, vlan_info={"vid": 100, "flags": expected_flags}
    )


# ---------------------------------------------------------------------------
# apply_vlan — trunk mode
# ---------------------------------------------------------------------------

def test_apply_vlan_trunk_native_is_untagged():
    ip = MagicMock()
    def _lookup(ifname):
        return {"nos-br": [10], "eth1": [3]}.get(ifname, [])
    ip.link_lookup.side_effect = lambda ifname: _lookup(ifname)
    ip.link.return_value = _link_get_response(master_idx=10)
    ip.vlan_filter.return_value = None

    driver = _make_driver(ip)
    driver.apply_vlan(
        "nos-br", "eth1",
        {"interface_mode": "trunk", "vlans": [100, 200], "native_vlan": 100},
    )

    native_flags = _BRIDGE_VLAN_INFO_PVID | _BRIDGE_VLAN_INFO_UNTAGGED
    ip.vlan_filter.assert_any_call(
        "add", index=3, vlan_info={"vid": 100, "flags": native_flags}
    )
    ip.vlan_filter.assert_any_call(
        "add", index=3, vlan_info={"vid": 200, "flags": 0}
    )


def test_apply_vlan_raises_if_bridge_missing():
    ip = MagicMock()
    ip.link_lookup.return_value = []

    driver = _make_driver(ip)
    with pytest.raises(ValueError, match="does not exist"):
        driver.apply_vlan("nos-br", "eth1", {})


# ---------------------------------------------------------------------------
# delete_bridge
# ---------------------------------------------------------------------------

def test_delete_bridge_brings_down_and_removes():
    ip = MagicMock()
    ip.link_lookup.return_value = [10]
    ip.link.return_value = []

    driver = _make_driver(ip)
    driver.delete_bridge("nos-br")

    ip.link.assert_any_call("set", index=10, state="down")
    ip.link.assert_any_call("del", index=10)


def test_delete_bridge_noop_when_not_found():
    ip = MagicMock()
    ip.link_lookup.return_value = []
    driver = _make_driver(ip)
    driver.delete_bridge("nos-br")
    ip.link.assert_not_called()


# ---------------------------------------------------------------------------
# get_bridge_ports
# ---------------------------------------------------------------------------

def _make_link(master_idx, ifname):
    link = MagicMock()
    link.get_attr.side_effect = lambda k: master_idx if k == "IFLA_MASTER" else (ifname if k == "IFLA_IFNAME" else None)
    return link


def test_get_bridge_ports_returns_attached_interfaces():
    ip = MagicMock()
    ip.link_lookup.return_value = [10]
    ip.get_links.return_value = [
        _make_link(10, "eth0"),
        _make_link(10, "eth1"),
        _make_link(99, "eth2"),  # attached to a different bridge
    ]

    driver = _make_driver(ip)
    ports = driver.get_bridge_ports("nos-br")

    assert sorted(ports) == ["eth0", "eth1"]


def test_get_bridge_ports_returns_empty_when_bridge_absent():
    ip = MagicMock()
    ip.link_lookup.return_value = []

    driver = _make_driver(ip)
    ports = driver.get_bridge_ports("nos-br")

    assert ports == []
    ip.get_links.assert_not_called()


def test_get_bridge_ports_returns_empty_when_no_members():
    ip = MagicMock()
    ip.link_lookup.return_value = [10]
    ip.get_links.return_value = [
        _make_link(0, "lo"),
        _make_link(99, "eth3"),
    ]

    driver = _make_driver(ip)
    ports = driver.get_bridge_ports("nos-br")

    assert ports == []
