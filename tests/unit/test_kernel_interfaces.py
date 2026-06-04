"""Unit tests for nos.drivers.kernel.interfaces.InterfaceDriver."""
import json
from unittest.mock import MagicMock, call

import pytest

import nos.drivers.kernel.interfaces as _iface_mod
from nos.drivers.kernel.interfaces import InterfaceDriver


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def _clear_nos_managed(monkeypatch, tmp_path):
    """Reset tracking dict between tests and redirect disk I/O to a temp path."""
    _iface_mod._nos_managed_addresses.clear()
    monkeypatch.setattr(_iface_mod, "_STATE_FILE", tmp_path / "managed.json")
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
# lo0 loopback dummy — apply_interface
# ---------------------------------------------------------------------------

def test_lo0_apply_interface_creates_dummy_when_absent():
    """lo0 must be created as a dummy interface when it does not exist in kernel."""
    ip = MagicMock()
    ip.link_lookup.side_effect = [[], [6]]  # not found, then found after creation
    ip.addr.return_value = []
    ip.link.return_value = []

    driver = _make_driver(ip)
    driver.apply_interface("lo0", {})

    ip.link.assert_any_call("add", ifname="lo0", kind="dummy")


def test_lo0_apply_interface_reuses_existing_dummy():
    """If lo0 already exists, no link add should be called."""
    ip = _make_ip(ifindex=7)
    driver = _make_driver(ip)
    driver.apply_interface("lo0", {})

    for c in ip.link.call_args_list:
        assert c.args[0] != "add", "Should not re-create an existing lo0"


def test_lo0_apply_interface_assigns_loopback_address():
    """lo0 with family_inet address should have the address applied."""
    ip = MagicMock()
    ip.link_lookup.side_effect = [[], [6]]
    ip.addr.return_value = []
    ip.link.return_value = []

    driver = _make_driver(ip)
    driver.apply_interface("lo0", {"family_inet": {"address": {"1.1.1.1/32": {}}}})

    ip.addr.assert_any_call("add", index=6, address="1.1.1.1", prefixlen=32)


def test_lo0_apply_interface_assigns_ipv6_loopback_address():
    """lo0 with family_inet6 address should have the IPv6 address applied."""
    ip = MagicMock()
    ip.link_lookup.side_effect = [[], [6]]
    ip.addr.return_value = []
    ip.link.return_value = []

    driver = _make_driver(ip)
    driver.apply_interface("lo0", {"family_inet6": {"address": {"::1/128": {}}}})

    ip.addr.assert_any_call("add", index=6, address="::1", prefixlen=128)


def test_plain_lo_is_still_physical_not_created(caplog):
    """Plain 'lo' (system loopback) must not be created — it is physical."""
    ip = MagicMock()
    ip.link_lookup.return_value = []  # not found

    driver = _make_driver(ip)
    with caplog.at_level("WARNING"):
        driver.apply_interface("lo", {})

    assert "not found" in caplog.text
    ip.link.assert_not_called()


# ---------------------------------------------------------------------------
# lo0 loopback dummy — delete_interface
# ---------------------------------------------------------------------------

def test_lo0_delete_interface_removes_dummy():
    """delete_interface('lo0') must delete the dummy kernel interface."""
    ip = _make_ip(ifindex=6)
    driver = _make_driver(ip)
    driver.delete_interface("lo0")

    ip.link.assert_any_call("set", index=6, state="down")
    ip.link.assert_any_call("del", index=6)


def test_lo0_delete_interface_noop_when_not_found():
    """delete_interface('lo0') is a no-op when lo0 is absent from kernel."""
    ip = MagicMock()
    ip.link_lookup.return_value = []

    driver = _make_driver(ip)
    driver.delete_interface("lo0")

    ip.link.assert_not_called()


# ---------------------------------------------------------------------------
# lo0 loopback dummy — apply_subinterface (loopback units)
# ---------------------------------------------------------------------------

def test_lo0_unit0_applies_address_to_parent():
    """Unit 0 on lo0 maps to lo0 itself — addresses go on lo0, no link add."""
    ip = _make_ip(ifindex=6)
    driver = _make_driver(ip)
    driver.apply_subinterface("lo0", 0, {"family_inet": {"address": {"10.0.0.1/32": {}}}})

    # No new link should be created
    for c in ip.link.call_args_list:
        assert c.args[0] != "add", "Unit 0 must not create a kernel subinterface"

    ip.addr.assert_any_call("add", index=6, address="10.0.0.1", prefixlen=32)


def test_lo0_unit0_state_up_when_not_disabled():
    """Unit 0 on lo0 brings lo0 up."""
    ip = _make_ip(ifindex=6)
    driver = _make_driver(ip)
    driver.apply_subinterface("lo0", 0, {})

    ip.link.assert_any_call("set", index=6, state="up")


def test_lo0_unit0_warns_when_parent_absent(caplog):
    """Unit 0 on lo0 logs a warning when lo0 doesn't exist yet."""
    ip = MagicMock()
    ip.link_lookup.return_value = []

    driver = _make_driver(ip)
    with caplog.at_level("WARNING"):
        driver.apply_subinterface("lo0", 0, {})

    assert "lo0" in caplog.text
    ip.link.assert_not_called()


def test_lo0_unit1_creates_new_dummy():
    """Unit 1 on lo0 creates a separate dummy interface lo0.1."""
    ip = MagicMock()
    ip.link_lookup.side_effect = [
        [],   # lo0.1 not found
        [8],  # lo0.1 found after creation
    ]
    ip.addr.return_value = []
    ip.link.return_value = []

    driver = _make_driver(ip)
    driver.apply_subinterface("lo0", 1, {})

    ip.link.assert_any_call("add", ifname="lo0.1", kind="dummy")


def test_lo0_unit1_applies_address():
    """Unit 1 on lo0 assigns address to lo0.1."""
    ip = MagicMock()
    ip.link_lookup.side_effect = [[], [8]]
    ip.addr.return_value = []
    ip.link.return_value = []

    driver = _make_driver(ip)
    driver.apply_subinterface("lo0", 1, {"family_inet": {"address": {"2.2.2.2/32": {}}}})

    ip.addr.assert_any_call("add", index=8, address="2.2.2.2", prefixlen=32)


def test_lo0_unit1_reuses_existing_dummy():
    """If lo0.1 already exists, no link add should be called."""
    ip = MagicMock()
    ip.link_lookup.return_value = [8]  # lo0.1 already present
    ip.addr.return_value = []
    ip.link.return_value = []

    driver = _make_driver(ip)
    driver.apply_subinterface("lo0", 1, {})

    for c in ip.link.call_args_list:
        assert c.args[0] != "add", "Should not re-create an existing lo0.1"


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


# ---------------------------------------------------------------------------
# clear_nos_addresses
# ---------------------------------------------------------------------------

def _make_addr_msg(addr: str, plen: int) -> MagicMock:
    msg = MagicMock()
    msg.get_attr.return_value = addr
    msg.__getitem__ = lambda self, k, _plen=plen: _plen if k == "prefixlen" else None
    return msg


def test_clear_nos_addresses_removes_listed_inet_address():
    """Addresses in old_config are removed from the kernel."""
    nos_addr = _make_addr_msg("10.0.0.1", 30)

    ip = MagicMock()
    ip.link_lookup.return_value = [2]
    ip.addr.side_effect = lambda *a, **kw: [nos_addr] if a[0] == "dump" else None

    driver = _make_driver(ip)
    old_config = {"family_inet": {"address": {"10.0.0.1/30": {}}}}
    driver.clear_nos_addresses("eth0", old_config)

    ip.addr.assert_any_call("del", index=2, address="10.0.0.1", prefixlen=30)


def test_clear_nos_addresses_removes_listed_inet6_address():
    """IPv6 addresses in old_config are removed from the kernel."""
    nos_addr = _make_addr_msg("2001:db8::1", 64)

    ip = MagicMock()
    ip.link_lookup.return_value = [2]
    ip.addr.side_effect = lambda *a, **kw: [nos_addr] if a[0] == "dump" else None

    driver = _make_driver(ip)
    old_config = {"family_inet6": {"address": {"2001:db8::1/64": {}}}}
    driver.clear_nos_addresses("eth0", old_config)

    ip.addr.assert_any_call("del", index=2, address="2001:db8::1", prefixlen=64)


def test_clear_nos_addresses_leaves_unlisted_addresses_alone():
    """Addresses not in old_config must not be removed."""
    nos_addr = _make_addr_msg("10.0.0.1", 30)
    other_addr = _make_addr_msg("192.168.1.1", 24)

    ip = MagicMock()
    ip.link_lookup.return_value = [2]
    ip.addr.side_effect = lambda *a, **kw: [nos_addr, other_addr] if a[0] == "dump" else None

    driver = _make_driver(ip)
    old_config = {"family_inet": {"address": {"10.0.0.1/30": {}}}}
    driver.clear_nos_addresses("eth0", old_config)

    for c in ip.addr.call_args_list:
        assert not (c.args[0] == "del" and c.kwargs.get("address") == "192.168.1.1"), (
            "clear_nos_addresses must not remove addresses absent from old_config"
        )


def test_clear_nos_addresses_noop_when_old_config_has_no_addresses():
    """With no addresses in old_config, no addr('dump') or addr('del') is issued."""
    ip = MagicMock()
    ip.link_lookup.return_value = [2]

    driver = _make_driver(ip)
    driver.clear_nos_addresses("eth0", {})

    ip.link_lookup.assert_not_called()
    ip.addr.assert_not_called()


def test_clear_nos_addresses_noop_when_interface_missing(caplog):
    """Missing interface logs a warning and does nothing."""
    ip = MagicMock()
    ip.link_lookup.return_value = []

    driver = _make_driver(ip)
    with caplog.at_level("WARNING"):
        driver.clear_nos_addresses("eth0", {"family_inet": {"address": {"10.0.0.1/30": {}}}})

    assert "not found" in caplog.text
    ip.addr.assert_not_called()


def test_clear_nos_addresses_updates_nos_managed_tracking():
    """Cleared addresses are removed from the NOS-managed tracking set."""
    _iface_mod._nos_managed_addresses["eth0"] = {("10.0.0.1", 30), ("10.0.0.5", 30)}

    nos_addr = _make_addr_msg("10.0.0.1", 30)

    ip = MagicMock()
    ip.link_lookup.return_value = [2]
    ip.addr.side_effect = lambda *a, **kw: [nos_addr] if a[0] == "dump" else None

    driver = _make_driver(ip)
    driver.clear_nos_addresses("eth0", {"family_inet": {"address": {"10.0.0.1/30": {}}}})

    assert ("10.0.0.1", 30) not in _iface_mod._nos_managed_addresses["eth0"]
    assert ("10.0.0.5", 30) in _iface_mod._nos_managed_addresses["eth0"]


def test_clear_nos_addresses_removes_both_inet_and_inet6():
    """Both IPv4 and IPv6 addresses from old_config are removed."""
    v4_addr = _make_addr_msg("10.0.0.1", 30)
    v6_addr = _make_addr_msg("2001:db8::1", 64)

    ip = MagicMock()
    ip.link_lookup.return_value = [2]
    ip.addr.side_effect = lambda *a, **kw: [v4_addr, v6_addr] if a[0] == "dump" else None

    driver = _make_driver(ip)
    old_config = {
        "family_inet": {"address": {"10.0.0.1/30": {}}},
        "family_inet6": {"address": {"2001:db8::1/64": {}}},
    }
    driver.clear_nos_addresses("eth0", old_config)

    ip.addr.assert_any_call("del", index=2, address="10.0.0.1", prefixlen=30)
    ip.addr.assert_any_call("del", index=2, address="2001:db8::1", prefixlen=64)


# ---------------------------------------------------------------------------
# Persistence — _load_managed_addresses / _save_managed_addresses
# ---------------------------------------------------------------------------

def test_save_written_after_sync(tmp_path):
    """Syncing an address writes the tracking state to _STATE_FILE."""
    ip = _make_ip(existing_addrs=[])
    driver = _make_driver(ip)
    driver.sync_interface_addresses("eth0", {"family_inet": {"address": {"10.0.0.1/24": {}}}})

    state_file = tmp_path / "managed.json"
    assert state_file.exists(), "_STATE_FILE should be written after sync"
    data = json.loads(state_file.read_text())
    assert ["10.0.0.1", 24] in data["eth0"]


def test_save_not_written_on_early_return(tmp_path):
    """No file is written when _sync_addresses returns early (nothing to manage)."""
    ip = _make_ip()
    driver = _make_driver(ip)
    driver.sync_interface_addresses("eth0", {})  # empty config, no prior tracking

    assert not (tmp_path / "managed.json").exists()


def test_save_written_after_clear(tmp_path):
    """Clearing addresses writes the updated tracking state to _STATE_FILE."""
    _iface_mod._nos_managed_addresses["eth0"] = {("10.0.0.1", 30)}

    nos_addr = _make_addr_msg("10.0.0.1", 30)
    ip = MagicMock()
    ip.link_lookup.return_value = [2]
    ip.addr.side_effect = lambda *a, **kw: [nos_addr] if a[0] == "dump" else None

    driver = _make_driver(ip)
    driver.clear_nos_addresses("eth0", {"family_inet": {"address": {"10.0.0.1/30": {}}}})

    state_file = tmp_path / "managed.json"
    assert state_file.exists()
    data = json.loads(state_file.read_text())
    assert data["eth0"] == []


def test_load_populates_from_existing_file(tmp_path, monkeypatch):
    """_load_managed_addresses restores the tracking dict from a valid JSON file."""
    state_file = tmp_path / "managed.json"
    state_file.write_text(json.dumps({"eth0": [["10.0.0.1", 24], ["10.0.0.2", 24]]}))
    monkeypatch.setattr(_iface_mod, "_STATE_FILE", state_file)

    result = _iface_mod._load_managed_addresses()

    assert result == {"eth0": {("10.0.0.1", 24), ("10.0.0.2", 24)}}


def test_load_returns_empty_when_file_missing(tmp_path, monkeypatch):
    """_load_managed_addresses returns {} and logs no warning when file is absent."""
    monkeypatch.setattr(_iface_mod, "_STATE_FILE", tmp_path / "no_such_file.json")

    result = _iface_mod._load_managed_addresses()

    assert result == {}


def test_load_returns_empty_and_warns_on_corrupt_file(tmp_path, monkeypatch, caplog):
    """_load_managed_addresses returns {} and logs a warning for malformed JSON."""
    state_file = tmp_path / "managed.json"
    state_file.write_text("not valid json {{{")
    monkeypatch.setattr(_iface_mod, "_STATE_FILE", state_file)

    with caplog.at_level("WARNING"):
        result = _iface_mod._load_managed_addresses()

    assert result == {}
    assert "managed.json" in caplog.text


def test_save_creates_directory_if_missing(tmp_path, monkeypatch):
    """_save_managed_addresses creates the parent directory when it does not exist."""
    state_file = tmp_path / "subdir" / "managed.json"
    monkeypatch.setattr(_iface_mod, "_STATE_FILE", state_file)
    _iface_mod._nos_managed_addresses["lo"] = {("127.0.0.1", 8)}

    _iface_mod._save_managed_addresses()

    assert state_file.exists()


def test_save_content_round_trips_through_load(tmp_path, monkeypatch):
    """Data written by _save_managed_addresses is faithfully restored by _load."""
    state_file = tmp_path / "managed.json"
    monkeypatch.setattr(_iface_mod, "_STATE_FILE", state_file)
    _iface_mod._nos_managed_addresses["eth0"] = {("10.0.0.1", 30)}
    _iface_mod._nos_managed_addresses["eth1"] = {("10.0.1.1", 24), ("10.0.1.2", 24)}

    _iface_mod._save_managed_addresses()
    result = _iface_mod._load_managed_addresses()

    assert result == {
        "eth0": {("10.0.0.1", 30)},
        "eth1": {("10.0.1.1", 24), ("10.0.1.2", 24)},
    }


# ---------------------------------------------------------------------------
# clear_nos_addresses
# ---------------------------------------------------------------------------

def _make_addr_msg(addr: str, plen: int) -> MagicMock:
    msg = MagicMock()
    msg.get_attr.return_value = addr
    msg.__getitem__ = lambda self, k, _plen=plen: _plen if k == "prefixlen" else None
    return msg


def test_clear_nos_addresses_removes_listed_inet_address():
    """Addresses in old_config are removed from the kernel."""
    nos_addr = _make_addr_msg("10.0.0.1", 30)

    ip = MagicMock()
    ip.link_lookup.return_value = [2]
    ip.addr.side_effect = lambda *a, **kw: [nos_addr] if a[0] == "dump" else None

    driver = _make_driver(ip)
    old_config = {"family_inet": {"address": {"10.0.0.1/30": {}}}}
    driver.clear_nos_addresses("eth0", old_config)

    ip.addr.assert_any_call("del", index=2, address="10.0.0.1", prefixlen=30)


def test_clear_nos_addresses_removes_listed_inet6_address():
    """IPv6 addresses in old_config are removed from the kernel."""
    nos_addr = _make_addr_msg("2001:db8::1", 64)

    ip = MagicMock()
    ip.link_lookup.return_value = [2]
    ip.addr.side_effect = lambda *a, **kw: [nos_addr] if a[0] == "dump" else None

    driver = _make_driver(ip)
    old_config = {"family_inet6": {"address": {"2001:db8::1/64": {}}}}
    driver.clear_nos_addresses("eth0", old_config)

    ip.addr.assert_any_call("del", index=2, address="2001:db8::1", prefixlen=64)


def test_clear_nos_addresses_leaves_unlisted_addresses_alone():
    """Addresses not in old_config must not be removed."""
    nos_addr = _make_addr_msg("10.0.0.1", 30)
    other_addr = _make_addr_msg("192.168.1.1", 24)

    ip = MagicMock()
    ip.link_lookup.return_value = [2]
    ip.addr.side_effect = lambda *a, **kw: [nos_addr, other_addr] if a[0] == "dump" else None

    driver = _make_driver(ip)
    old_config = {"family_inet": {"address": {"10.0.0.1/30": {}}}}
    driver.clear_nos_addresses("eth0", old_config)

    for c in ip.addr.call_args_list:
        assert not (c.args[0] == "del" and c.kwargs.get("address") == "192.168.1.1"), (
            "clear_nos_addresses must not remove addresses absent from old_config"
        )


def test_clear_nos_addresses_noop_when_old_config_has_no_addresses():
    """With no addresses in old_config, no addr('dump') or addr('del') is issued."""
    ip = MagicMock()
    ip.link_lookup.return_value = [2]

    driver = _make_driver(ip)
    driver.clear_nos_addresses("eth0", {})

    ip.link_lookup.assert_not_called()
    ip.addr.assert_not_called()


def test_clear_nos_addresses_noop_when_interface_missing(caplog):
    """Missing interface logs a warning and does nothing."""
    ip = MagicMock()
    ip.link_lookup.return_value = []

    driver = _make_driver(ip)
    with caplog.at_level("WARNING"):
        driver.clear_nos_addresses("eth0", {"family_inet": {"address": {"10.0.0.1/30": {}}}})

    assert "not found" in caplog.text
    ip.addr.assert_not_called()


def test_clear_nos_addresses_updates_nos_managed_tracking():
    """Cleared addresses are removed from the NOS-managed tracking set."""
    _iface_mod._nos_managed_addresses["eth0"] = {("10.0.0.1", 30), ("10.0.0.5", 30)}

    nos_addr = _make_addr_msg("10.0.0.1", 30)

    ip = MagicMock()
    ip.link_lookup.return_value = [2]
    ip.addr.side_effect = lambda *a, **kw: [nos_addr] if a[0] == "dump" else None

    driver = _make_driver(ip)
    driver.clear_nos_addresses("eth0", {"family_inet": {"address": {"10.0.0.1/30": {}}}})

    assert ("10.0.0.1", 30) not in _iface_mod._nos_managed_addresses["eth0"]
    assert ("10.0.0.5", 30) in _iface_mod._nos_managed_addresses["eth0"]


def test_clear_nos_addresses_removes_both_inet_and_inet6():
    """Both IPv4 and IPv6 addresses from old_config are removed."""
    v4_addr = _make_addr_msg("10.0.0.1", 30)
    v6_addr = _make_addr_msg("2001:db8::1", 64)

    ip = MagicMock()
    ip.link_lookup.return_value = [2]
    ip.addr.side_effect = lambda *a, **kw: [v4_addr, v6_addr] if a[0] == "dump" else None

    driver = _make_driver(ip)
    old_config = {
        "family_inet": {"address": {"10.0.0.1/30": {}}},
        "family_inet6": {"address": {"2001:db8::1/64": {}}},
    }
    driver.clear_nos_addresses("eth0", old_config)

    ip.addr.assert_any_call("del", index=2, address="10.0.0.1", prefixlen=30)
    ip.addr.assert_any_call("del", index=2, address="2001:db8::1", prefixlen=64)
