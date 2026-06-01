"""Unit tests for nos.drivers.kernel.vrf.VRFDriver."""
from unittest.mock import MagicMock

import pytest

from nos.drivers.kernel.vrf import VRFDriver, vrf_table_id


# ---------------------------------------------------------------------------
# vrf_table_id
# ---------------------------------------------------------------------------

def test_vrf_table_id_is_deterministic():
    assert vrf_table_id("red") == vrf_table_id("red")


def test_vrf_table_id_different_names():
    assert vrf_table_id("red") != vrf_table_id("blue")


def test_vrf_table_id_in_range():
    for name in ("red", "blue", "mgmt", "default", "a" * 64):
        tid = vrf_table_id(name)
        assert 1000 <= tid <= 9999, f"{name!r} → {tid} out of range"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_driver(mock_ip):
    factory = MagicMock()
    factory.return_value.__enter__ = MagicMock(return_value=mock_ip)
    factory.return_value.__exit__ = MagicMock(return_value=False)
    return VRFDriver(iproute_factory=factory)


def _link_get(master=0):
    msg = MagicMock()
    msg.get_attr.side_effect = lambda k: master if k == "IFLA_MASTER" else None
    return [msg]


# ---------------------------------------------------------------------------
# apply_vrf
# ---------------------------------------------------------------------------

def test_apply_vrf_creates_vrf_device():
    ip = MagicMock()
    ip.link_lookup.side_effect = [[], [5]]  # not found, then found
    ip.link.return_value = _link_get()

    driver = _make_driver(ip)
    table = driver.apply_vrf("red")

    ip.link.assert_any_call("add", ifname="red", kind="vrf", vrf_table=table)


def test_apply_vrf_returns_table_id():
    ip = MagicMock()
    ip.link_lookup.return_value = [5]
    ip.link.return_value = _link_get()

    driver = _make_driver(ip)
    table = driver.apply_vrf("red")

    assert table == vrf_table_id("red")


def test_apply_vrf_brings_up():
    ip = MagicMock()
    ip.link_lookup.return_value = [5]
    ip.link.return_value = _link_get()

    driver = _make_driver(ip)
    driver.apply_vrf("red")

    ip.link.assert_any_call("set", index=5, state="up")


def test_apply_vrf_skips_creation_if_exists():
    ip = MagicMock()
    ip.link_lookup.return_value = [5]
    ip.link.return_value = _link_get()

    driver = _make_driver(ip)
    driver.apply_vrf("red")

    for c in ip.link.call_args_list:
        assert c.args[0] != "add", "Should not recreate existing VRF"


def test_apply_vrf_enslaves_interfaces():
    ip = MagicMock()
    def _lookup(ifname):
        return {"red": [5], "eth1": [3], "eth2": [4]}.get(ifname, [])
    ip.link_lookup.side_effect = lambda ifname: _lookup(ifname)
    ip.link.return_value = _link_get(master=0)

    driver = _make_driver(ip)
    driver.apply_vrf("red", interfaces=["eth1", "eth2"])

    ip.link.assert_any_call("set", index=3, master=5, state="up")
    ip.link.assert_any_call("set", index=4, master=5, state="up")


def test_apply_vrf_skips_already_enslaved():
    """Interfaces already enslaved to this VRF should not generate extra netlink calls."""
    ip = MagicMock()
    def _lookup(ifname):
        return {"red": [5], "eth1": [3]}.get(ifname, [])
    ip.link_lookup.side_effect = lambda ifname: _lookup(ifname)

    # link("get") returns IFLA_MASTER = 5 (already enslaved).
    ip.link.side_effect = lambda *a, **kw: (
        _link_get(master=5) if a[0] == "get" else None
    )

    driver = _make_driver(ip)
    driver.apply_vrf("red", interfaces=["eth1"])

    enslave_calls = [
        c for c in ip.link.call_args_list
        if c.args[0] == "set" and c.kwargs.get("master") == 5
    ]
    assert len(enslave_calls) == 0, "Should not re-enslave already enslaved interface"


# ---------------------------------------------------------------------------
# delete_vrf
# ---------------------------------------------------------------------------

def test_delete_vrf_brings_down_and_removes():
    ip = MagicMock()
    ip.link_lookup.return_value = [5]
    ip.link.return_value = []

    driver = _make_driver(ip)
    driver.delete_vrf("red")

    ip.link.assert_any_call("set", index=5, state="down")
    ip.link.assert_any_call("del", index=5)


def test_delete_vrf_noop_when_not_found():
    ip = MagicMock()
    ip.link_lookup.return_value = []
    driver = _make_driver(ip)
    driver.delete_vrf("nonexistent")
    ip.link.assert_not_called()


# ---------------------------------------------------------------------------
# assign_interface / release_interface
# ---------------------------------------------------------------------------

def test_assign_interface_enslaves_to_vrf():
    ip = MagicMock()
    def _lookup(ifname):
        return {"red": [5], "eth3": [6]}.get(ifname, [])
    ip.link_lookup.side_effect = lambda ifname: _lookup(ifname)
    ip.link.return_value = _link_get(master=0)

    driver = _make_driver(ip)
    driver.assign_interface("red", "eth3")

    ip.link.assert_any_call("set", index=6, master=5, state="up")


def test_assign_interface_raises_if_vrf_missing():
    ip = MagicMock()
    ip.link_lookup.return_value = []
    driver = _make_driver(ip)
    with pytest.raises(ValueError, match="does not exist"):
        driver.assign_interface("ghost", "eth0")


def test_release_interface_sets_master_zero():
    ip = MagicMock()
    ip.link_lookup.return_value = [3]
    ip.link.return_value = []

    driver = _make_driver(ip)
    driver.release_interface("eth1")

    ip.link.assert_called_with("set", index=3, master=0)
