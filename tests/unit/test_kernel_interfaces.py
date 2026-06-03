"""Unit tests for nos.drivers.kernel.interfaces.InterfaceDriver."""
from unittest.mock import MagicMock, call

import pytest

import nos.drivers.kernel.interfaces as _iface_mod
from nos.drivers.kernel.interfaces import InterfaceDriver


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def _clear_nos_managed():
    """Reset the module-level address-tracking dict between tests."""
    _iface_mod._nos_managed_addresses.clear()
    yield
    _iface_mod._nos_managed_addresses.clear()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_driver(mock_ip):
    """Return an InterfaceDriver wired to *mock_ip* as the IPRoute instance."""
    factory = MagicMock()
    factory.return_value.__enter__ = MagicMock(return_value=mock_ip)
    factory.return_value.__exit__ = MagicMock(return_value=False)
    return InterfaceDriver(iproute_factory=factory)


def _make_ip(ifindex=2, existing_addrs=None):
    """Return a preconfigured IPRoute mock."""
    ip = MagicMock()
    ip.link_lookup.return_value = [ifindex]
    # addr("dump") returns list of message mocks.
    addr_msgs = []
    for addr, plen in (existing_addrs or []):
        msg = MagicMock()
        msg.get_attr.return_value = addr
        msg.__getitem__ = lambda self, k, _plen=plen: _plen if k == "prefixlen" else None
        addr_msgs.append(msg)
    ip.addr.return_value = addr_msgs
    ip.link.return_value = []
    return ip


# ---------------------------------------------------------------------------
# apply_interface — basic attributes
# ---------------------------------------------------------------------------

def test_apply_interface_sets_mtu():
    ip = _make_ip()
    driver = _make_driver(ip)
    driver.apply_interface("eth0", {"mtu": 9000})
    ip.link.assert_any_call("set", index=2, mtu=9000)


def test_apply_interface_sets_description():
    ip = _make_ip()
    driver = _make_driver(ip)
    driver.apply_interface("eth0", {"description": "uplink"})
    ip.link.assert_any_call("set", index=2, ifalias="uplink")


def test_apply_interface_sets_both_mtu_and_description():
    ip = _make_ip()
    driver = _make_driver(ip)
    driver.apply_interface("eth0", {"mtu": 1500, "description": "core"})
    ip.link.assert_any_call("set", index=2, ifalias="core", mtu=1500)


def test_apply_interface_brings_up_by_default():
    ip = _make_ip()
    driver = _make_driver(ip)
    driver.apply_interface("eth0", {})
    ip.link.assert_any_call("set", index=2, state="up")


def test_apply_interface_disable_brings_down():
    ip = _make_ip()
    driver = _make_driver(ip)
    driver.apply_interface("eth0", {"disable": True})
    ip.link.assert_any_call("set", index=2, state="down")


# ---------------------------------------------------------------------------
# apply_interface — IP address management
# ---------------------------------------------------------------------------

def test_apply_interface_adds_new_address():
    ip = _make_ip(existing_addrs=[])
    driver = _make_driver(ip)
    driver.apply_interface("eth0", {"family_inet": {"address": {"10.0.0.1/30": {}}}})
    ip.addr.assert_any_call("add", index=2, address="10.0.0.1", prefixlen=30)


def test_apply_interface_no_duplicate_add():
    """If address is already present, no addr('add') should be issued."""
    existing = MagicMock()
    existing.get_attr.return_value = "10.0.0.1"
    existing.__getitem__ = lambda self, k: 30 if k == "prefixlen" else None

    ip = MagicMock()
    ip.link_lookup.return_value = [2]
    ip.addr.side_effect = lambda *a, **kw: [existing] if a[0] == "dump" else None
    ip.link.return_value = []

    driver = _make_driver(ip)
    driver.apply_interface("eth0", {"family_inet": {"address": {"10.0.0.1/30": {}}}})

    for c in ip.addr.call_args_list:
        assert c.args[0] != "add", "Should not re-add existing address"


def test_apply_interface_does_not_remove_os_address():
    """An address NOS never applied must not be removed, even when NOS configures a different one."""
    os_addr = MagicMock()
    os_addr.get_attr.return_value = "192.168.1.1"
    os_addr.__getitem__ = lambda self, k: 24 if k == "prefixlen" else None

    ip = MagicMock()
    ip.link_lookup.return_value = [2]
    ip.addr.side_effect = lambda *a, **kw: [os_addr] if a[0] == "dump" else None
    ip.link.return_value = []

    driver = _make_driver(ip)
    driver.apply_interface("eth0", {"family_inet": {"address": {"10.0.0.1/30": {}}}})

    for c in ip.addr.call_args_list:
        assert not (c.args[0] == "del" and c.kwargs.get("address") == "192.168.1.1"), (
            "NOS must not remove an address it did not apply"
        )


# ---------------------------------------------------------------------------
# apply_interface — missing physical interface
# ---------------------------------------------------------------------------

def test_apply_interface_skips_missing_physical(caplog):
    ip = MagicMock()
    ip.link_lookup.return_value = []  # not found

    driver = _make_driver(ip)
    with caplog.at_level("WARNING"):
        driver.apply_interface("eth0", {"mtu": 1500})

    assert "not found" in caplog.text
    ip.link.assert_not_called()


# ---------------------------------------------------------------------------
# apply_interface — virtual interface creation
# ---------------------------------------------------------------------------

def test_apply_interface_creates_dummy_for_unknown():
    ip = MagicMock()
    # First lookup returns nothing (doesn't exist), second returns index after creation.
    ip.link_lookup.side_effect = [[], [5]]
    ip.addr.return_value = []
    ip.link.return_value = []

    driver = _make_driver(ip)
    driver.apply_interface("dummy0", {})

    ip.link.assert_any_call("add", ifname="dummy0", kind="dummy")


# ---------------------------------------------------------------------------
# delete_interface
# ---------------------------------------------------------------------------

def test_delete_interface_removes_virtual():
    ip = _make_ip(ifindex=3)
    driver = _make_driver(ip)
    driver.delete_interface("dummy0")

    ip.link.assert_any_call("set", index=3, state="down")
    ip.link.assert_any_call("del", index=3)


def test_delete_interface_skips_physical(caplog):
    ip = _make_ip()
    driver = _make_driver(ip)
    driver.delete_interface("eth0")

    # Should not call any link operations on a physical interface.
    ip.link.assert_not_called()


def test_delete_interface_noop_when_not_found():
    ip = MagicMock()
    ip.link_lookup.return_value = []
    driver = _make_driver(ip)
    driver.delete_interface("dummy99")
    ip.link.assert_not_called()


# ---------------------------------------------------------------------------
# sync_interface_addresses
# ---------------------------------------------------------------------------

def test_sync_interface_addresses_adds_new_address():
    ip = _make_ip(existing_addrs=[])
    driver = _make_driver(ip)
    driver.sync_interface_addresses("eth0", {"family_inet": {"address": {"10.0.0.1/24": {}}}})
    ip.addr.assert_any_call("add", index=2, address="10.0.0.1", prefixlen=24)


def test_sync_interface_addresses_does_not_remove_os_address():
    """Empty NOS config must not remove addresses set by the OS (netplan/DHCP)."""
    os_addr = MagicMock()
    os_addr.get_attr.return_value = "10.1.1.1"
    os_addr.__getitem__ = lambda self, k: 24 if k == "prefixlen" else None

    ip = MagicMock()
    ip.link_lookup.return_value = [2]
    ip.addr.side_effect = lambda *a, **kw: [os_addr] if a[0] == "dump" else None
    ip.link.return_value = []

    driver = _make_driver(ip)
    driver.sync_interface_addresses("eth0", {})
    # addr("dump") must not even be called — we return early
    for c in ip.addr.call_args_list:
        assert c.args[0] != "del", "NOS must not remove an address it did not apply"


def test_sync_interface_addresses_does_not_touch_link_state():
    ip = _make_ip()
    driver = _make_driver(ip)
    driver.sync_interface_addresses("eth0", {"family_inet": {"address": {"10.0.0.1/24": {}}}})
    # link("set", ..., state=...) must NOT be called
    for c in ip.link.call_args_list:
        assert "state" not in c.kwargs, "sync_interface_addresses must not change link state"


def test_sync_interface_addresses_noop_when_interface_missing(caplog):
    ip = MagicMock()
    ip.link_lookup.return_value = []
    driver = _make_driver(ip)
    with caplog.at_level("WARNING"):
        driver.sync_interface_addresses("eth0", {"family_inet": {"address": {"10.0.0.1/24": {}}}})
    assert "not found" in caplog.text
    ip.addr.assert_not_called()


# ---------------------------------------------------------------------------
# apply_subinterface
# ---------------------------------------------------------------------------

def test_apply_subinterface_creates_vlan_link():
    ip = MagicMock()
    # parent found; subinterface not found initially, then found after creation
    ip.link_lookup.side_effect = [
        [3],   # parent lookup
        [],    # sub lookup before create
        [7],   # sub lookup after create
    ]
    ip.addr.return_value = []
    ip.link.return_value = []

    driver = _make_driver(ip)
    driver.apply_subinterface("ens34", 100, {"vlan_id": 100})

    ip.link.assert_any_call("add", ifname="ens34.100", kind="vlan", link=3, vlan_id=100)


def test_apply_subinterface_reuses_existing_link():
    ip = MagicMock()
    ip.link_lookup.side_effect = [
        [3],  # parent
        [9],  # subinterface already exists
    ]
    ip.addr.return_value = []
    ip.link.return_value = []

    driver = _make_driver(ip)
    driver.apply_subinterface("ens34", 100, {"vlan_id": 100})

    for c in ip.link.call_args_list:
        assert c.args[0] != "add", "Should not re-create an existing subinterface"


def test_apply_subinterface_applies_ip():
    ip = MagicMock()
    ip.link_lookup.side_effect = [[3], [], [7]]
    ip.addr.return_value = []
    ip.link.return_value = []

    driver = _make_driver(ip)
    driver.apply_subinterface(
        "ens34", 100,
        {"vlan_id": 100, "family_inet": {"address": {"192.168.100.1/24": {}}}},
    )
    ip.addr.assert_any_call("add", index=7, address="192.168.100.1", prefixlen=24)


def test_apply_subinterface_sets_state_up():
    ip = MagicMock()
    ip.link_lookup.side_effect = [[3], [], [7]]
    ip.addr.return_value = []
    ip.link.return_value = []

    driver = _make_driver(ip)
    driver.apply_subinterface("ens34", 100, {"vlan_id": 100})
    ip.link.assert_any_call("set", index=7, state="up")


def test_apply_subinterface_sets_state_down_when_disabled():
    ip = MagicMock()
    ip.link_lookup.side_effect = [[3], [], [7]]
    ip.addr.return_value = []
    ip.link.return_value = []

    driver = _make_driver(ip)
    driver.apply_subinterface("ens34", 100, {"vlan_id": 100, "disable": True})
    ip.link.assert_any_call("set", index=7, state="down")


def test_apply_subinterface_skips_when_parent_missing(caplog):
    ip = MagicMock()
    ip.link_lookup.return_value = []

    driver = _make_driver(ip)
    with caplog.at_level("WARNING"):
        driver.apply_subinterface("ens34", 100, {"vlan_id": 100})
    assert "not found" in caplog.text
    ip.link.assert_not_called()


def test_apply_subinterface_skips_when_no_vlan_id(caplog):
    ip = MagicMock()
    ip.link_lookup.side_effect = [
        [3],  # parent found
        [],   # subinterface not found
    ]
    ip.addr.return_value = []
    ip.link.return_value = []

    driver = _make_driver(ip)
    with caplog.at_level("WARNING"):
        driver.apply_subinterface("ens34", 100, {})
    assert "vlan_id" in caplog.text
    for c in ip.link.call_args_list:
        assert c.args[0] != "add"


# ---------------------------------------------------------------------------
# NOS-managed address tracking — non-interference with OS addresses
# ---------------------------------------------------------------------------

def test_nos_managed_address_is_removed_when_not_in_new_config():
    """Address NOS previously applied must be removed when dropped from config."""
    # Seed: NOS previously applied 192.168.1.1/24
    _iface_mod._nos_managed_addresses["eth0"] = {("192.168.1.1", 24)}

    stale = MagicMock()
    stale.get_attr.return_value = "192.168.1.1"
    stale.__getitem__ = lambda self, k: 24 if k == "prefixlen" else None

    ip = MagicMock()
    ip.link_lookup.return_value = [2]
    ip.addr.side_effect = lambda *a, **kw: [stale] if a[0] == "dump" else None
    ip.link.return_value = []

    driver = _make_driver(ip)
    driver.apply_interface("eth0", {"family_inet": {"address": {"10.0.0.1/30": {}}}})

    ip.addr.assert_any_call("del", index=2, address="192.168.1.1", prefixlen=24)


def test_sync_removes_nos_managed_address_when_config_cleared():
    """sync_interface_addresses with empty config removes NOS-managed addresses."""
    _iface_mod._nos_managed_addresses["eth0"] = {("10.1.1.1", 24)}

    nos_addr = MagicMock()
    nos_addr.get_attr.return_value = "10.1.1.1"
    nos_addr.__getitem__ = lambda self, k: 24 if k == "prefixlen" else None

    ip = MagicMock()
    ip.link_lookup.return_value = [2]
    ip.addr.side_effect = lambda *a, **kw: [nos_addr] if a[0] == "dump" else None
    ip.link.return_value = []

    driver = _make_driver(ip)
    driver.sync_interface_addresses("eth0", {})

    ip.addr.assert_any_call("del", index=2, address="10.1.1.1", prefixlen=24)


def test_os_address_preserved_when_nos_config_is_empty_and_never_managed():
    """When NOS has no config and has never touched the interface, addr dump is not called."""
    ip = MagicMock()
    ip.link_lookup.return_value = [2]
    ip.link.return_value = []

    driver = _make_driver(ip)
    driver.apply_interface("eth0", {})

    for c in ip.addr.call_args_list:
        assert c.args[0] != "dump", "addr dump must not run when NOS has no config to apply"


def test_nos_does_not_remove_os_address_when_applying_alongside_it():
    """Adding a NOS address must not disturb an OS-set address on the same interface."""
    os_addr = MagicMock()
    os_addr.get_attr.return_value = "172.16.0.1"
    os_addr.__getitem__ = lambda self, k: 16 if k == "prefixlen" else None

    ip = MagicMock()
    ip.link_lookup.return_value = [2]
    ip.addr.side_effect = lambda *a, **kw: [os_addr] if a[0] == "dump" else None
    ip.link.return_value = []

    driver = _make_driver(ip)
    driver.apply_interface("eth0", {"family_inet": {"address": {"10.0.0.1/30": {}}}})

    for c in ip.addr.call_args_list:
        assert not (c.args[0] == "del" and c.kwargs.get("address") == "172.16.0.1"), (
            "NOS must not remove the OS-assigned address"
        )


def test_nos_managed_set_updated_correctly_after_address_change():
    """After a config change, only the new address should be in the managed set."""
    ip = MagicMock()
    ip.link_lookup.return_value = [2]
    ip.addr.return_value = []
    ip.link.return_value = []

    driver = _make_driver(ip)
    driver.apply_interface("eth0", {"family_inet": {"address": {"10.0.0.1/30": {}}}})

    assert _iface_mod._nos_managed_addresses.get("eth0") == {("10.0.0.1", 30)}

    # Second apply with different address
    ip.addr.side_effect = lambda *a, **kw: [] if a[0] == "dump" else None
    driver.apply_interface("eth0", {"family_inet": {"address": {"10.0.0.5/30": {}}}})

    assert _iface_mod._nos_managed_addresses.get("eth0") == {("10.0.0.5", 30)}


# ---------------------------------------------------------------------------
# apply_svi
# ---------------------------------------------------------------------------

def test_apply_svi_creates_vlan_link_on_bridge():
    ip = MagicMock()
    ip.link_lookup.side_effect = [
        [10],  # nos-br
        [],    # irb.101 not found
        [15],  # irb.101 after create
    ]
    ip.addr.return_value = []
    ip.link.return_value = []

    driver = _make_driver(ip)
    driver.apply_svi("irb.101", {"vlan_id": 101})

    ip.link.assert_any_call("add", ifname="irb.101", kind="vlan", link=10, vlan_id=101)


def test_apply_svi_derives_vlan_id_from_name():
    """vlan_id inferred from irb.101 when not present in config."""
    ip = MagicMock()
    ip.link_lookup.side_effect = [[10], [], [15]]
    ip.addr.return_value = []
    ip.link.return_value = []

    driver = _make_driver(ip)
    driver.apply_svi("irb.101", {})

    ip.link.assert_any_call("add", ifname="irb.101", kind="vlan", link=10, vlan_id=101)


def test_apply_svi_reuses_existing_link():
    ip = MagicMock()
    ip.link_lookup.side_effect = [
        [10],  # nos-br
        [15],  # irb.101 already exists
    ]
    ip.addr.return_value = []
    ip.link.return_value = []

    driver = _make_driver(ip)
    driver.apply_svi("irb.101", {"vlan_id": 101})

    for c in ip.link.call_args_list:
        assert c.args[0] != "add", "Should not re-create an existing SVI"


def test_apply_svi_applies_inet_address():
    ip = MagicMock()
    ip.link_lookup.side_effect = [[10], [], [15]]
    ip.addr.return_value = []
    ip.link.return_value = []

    driver = _make_driver(ip)
    driver.apply_svi(
        "irb.101",
        {"vlan_id": 101, "family_inet": {"address": {"10.0.101.1/24": {}}}},
    )
    ip.addr.assert_any_call("add", index=15, address="10.0.101.1", prefixlen=24)


def test_apply_svi_applies_inet6_address():
    ip = MagicMock()
    ip.link_lookup.side_effect = [[10], [], [15]]
    ip.addr.return_value = []
    ip.link.return_value = []

    driver = _make_driver(ip)
    driver.apply_svi(
        "irb.101",
        {"vlan_id": 101, "family_inet6": {"address": {"2001:db8::1/64": {}}}},
    )
    ip.addr.assert_any_call("add", index=15, address="2001:db8::1", prefixlen=64)


def test_apply_svi_sets_state_up():
    ip = MagicMock()
    ip.link_lookup.side_effect = [[10], [], [15]]
    ip.addr.return_value = []
    ip.link.return_value = []

    driver = _make_driver(ip)
    driver.apply_svi("irb.101", {"vlan_id": 101})
    ip.link.assert_any_call("set", index=15, state="up")


def test_apply_svi_sets_state_down_when_disabled():
    ip = MagicMock()
    ip.link_lookup.side_effect = [[10], [], [15]]
    ip.addr.return_value = []
    ip.link.return_value = []

    driver = _make_driver(ip)
    driver.apply_svi("irb.101", {"vlan_id": 101, "disable": True})
    ip.link.assert_any_call("set", index=15, state="down")


def test_apply_svi_skips_when_bridge_missing(caplog):
    ip = MagicMock()
    ip.link_lookup.return_value = []

    driver = _make_driver(ip)
    with caplog.at_level("WARNING"):
        driver.apply_svi("irb.101", {"vlan_id": 101})
    assert "nos-br" in caplog.text
    ip.link.assert_not_called()


def test_apply_svi_skips_when_no_vlan_id_derivable(caplog):
    ip = MagicMock()

    driver = _make_driver(ip)
    with caplog.at_level("WARNING"):
        driver.apply_svi("irb", {})
    assert "vlan_id" in caplog.text
    ip.link_lookup.assert_not_called()
