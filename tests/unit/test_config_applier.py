"""Unit tests for nos.config.applier.ConfigApplier."""
from unittest.mock import MagicMock, call, patch

import pytest

from nos.config.applier import ConfigApplier, ConfigApplyError


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------

def _make_applier(pfe_available: bool = True):
    kernel = MagicMock()
    frr = MagicMock()
    pfe = MagicMock()
    pfe.is_available.return_value = pfe_available
    pfe.fib = MagicMock()
    return ConfigApplier(kernel, frr, pfe), kernel, frr, pfe


_IFACE_CFG = {"description": "uplink", "mtu": 1500}
_VLAN_CFG = {"vlan_id": 100, "description": "corp"}
_VLAN_SVI_CFG = {"vlan_id": 101, "l3_interface": "irb.101"}
_ROUTE_CFG = {"next_hop": "10.0.0.1"}


# ---------------------------------------------------------------------------
# Interfaces section
# ---------------------------------------------------------------------------

class TestInterfaces:
    def test_new_interface_calls_apply(self):
        applier, kernel, _, _ = _make_applier()
        applier.apply({}, {"interfaces": {"eth0": _IFACE_CFG}})
        kernel.apply_interface.assert_called_once_with("eth0", _IFACE_CFG)

    def test_removed_interface_calls_delete(self):
        applier, kernel, _, _ = _make_applier()
        applier.apply({"interfaces": {"eth0": _IFACE_CFG}}, {})
        kernel.delete_interface.assert_called_once_with("eth0")

    def test_unchanged_interface_still_reapplied(self):
        """Commit always enforces config, even when it hasn't changed."""
        applier, kernel, _, _ = _make_applier()
        config = {"interfaces": {"eth0": _IFACE_CFG}}
        applier.apply(config, config)
        kernel.apply_interface.assert_called_once_with("eth0", _IFACE_CFG)
        kernel.delete_interface.assert_not_called()

    def test_changed_interface_calls_apply(self):
        applier, kernel, _, _ = _make_applier()
        old = {"interfaces": {"eth0": {"description": "old"}}}
        new = {"interfaces": {"eth0": {"description": "new"}}}
        applier.apply(old, new)
        kernel.apply_interface.assert_called_once_with("eth0", {"description": "new"})

    def test_multiple_interfaces_each_handled(self):
        applier, kernel, _, _ = _make_applier()
        old = {"interfaces": {"eth0": _IFACE_CFG}}
        new = {"interfaces": {"eth0": _IFACE_CFG, "eth1": {"description": "b"}}}
        applier.apply(old, new)
        kernel.delete_interface.assert_not_called()
        assert kernel.apply_interface.call_count == 2
        kernel.apply_interface.assert_any_call("eth0", _IFACE_CFG)
        kernel.apply_interface.assert_any_call("eth1", {"description": "b"})

    def test_none_config_treated_as_empty_dict(self):
        applier, kernel, _, _ = _make_applier()
        applier.apply({"interfaces": {"eth0": None}}, {"interfaces": {"eth0": None}})
        kernel.apply_interface.assert_called_once_with("eth0", {})


# ---------------------------------------------------------------------------
# Interface units
# ---------------------------------------------------------------------------

class TestInterfaceUnits:
    def _iface(self, name: str, units: dict) -> dict:
        return {"interfaces": {name: {"unit": units}}}

    def test_unit_0_new_address_calls_sync(self):
        applier, kernel, _, _ = _make_applier()
        unit_cfg = {"family_inet": {"address": {"10.0.0.1/30": {}}}}
        applier.apply({}, self._iface("ens34", {"0": unit_cfg}))
        kernel.sync_interface_addresses.assert_called_once_with("ens34", unit_cfg)

    def test_unit_0_removed_clears_addresses(self):
        applier, kernel, _, _ = _make_applier()
        unit_cfg = {"family_inet": {"address": {"10.0.0.1/30": {}}}}
        applier.apply(self._iface("ens34", {"0": unit_cfg}), self._iface("ens34", {}))
        kernel.sync_interface_addresses.assert_called_once_with("ens34", {})

    def test_unit_0_unchanged_not_resynced(self):
        applier, kernel, _, _ = _make_applier()
        config = self._iface("ens34", {"0": {"family_inet": {"address": {"10.0.0.1/30": {}}}}})
        applier.apply(config, config)
        kernel.sync_interface_addresses.assert_not_called()

    def test_unit_n_new_calls_apply_subinterface(self):
        applier, kernel, _, _ = _make_applier()
        unit_cfg = {"vlan_id": 100, "family_inet": {"address": {"192.168.100.1/24": {}}}}
        applier.apply({}, self._iface("ens34", {"100": unit_cfg}))
        kernel.apply_subinterface.assert_called_once_with("ens34", 100, unit_cfg)

    def test_unit_n_removed_calls_delete_interface(self):
        applier, kernel, _, _ = _make_applier()
        unit_cfg = {"vlan_id": 100}
        applier.apply(self._iface("ens34", {"100": unit_cfg}), self._iface("ens34", {}))
        kernel.delete_interface.assert_called_once_with("ens34.100")

    def test_unit_n_unchanged_not_reapplied(self):
        applier, kernel, _, _ = _make_applier()
        config = self._iface("ens34", {"100": {"vlan_id": 100}})
        applier.apply(config, config)
        kernel.apply_subinterface.assert_not_called()

    def test_multiple_units_each_handled(self):
        applier, kernel, _, _ = _make_applier()
        units = {
            "0": {"family_inet": {"address": {"10.0.0.1/30": {}}}},
            "100": {"vlan_id": 100},
            "200": {"vlan_id": 200},
        }
        applier.apply({}, self._iface("ens34", units))
        kernel.sync_interface_addresses.assert_called_once_with(
            "ens34", {"family_inet": {"address": {"10.0.0.1/30": {}}}}
        )
        assert kernel.apply_subinterface.call_count == 2
        kernel.apply_subinterface.assert_any_call("ens34", 100, {"vlan_id": 100})
        kernel.apply_subinterface.assert_any_call("ens34", 200, {"vlan_id": 200})

    def test_interface_deleted_also_deletes_subinterfaces(self):
        applier, kernel, _, _ = _make_applier()
        old = self._iface("ens34", {"100": {"vlan_id": 100}, "200": {"vlan_id": 200}})
        applier.apply(old, {})
        kernel.delete_interface.assert_any_call("ens34.100")
        kernel.delete_interface.assert_any_call("ens34.200")
        kernel.delete_interface.assert_any_call("ens34")

    def test_interface_deleted_with_unit_0_clears_addresses_first(self):
        applier, kernel, _, _ = _make_applier()
        unit_cfg = {"family_inet": {"address": {"10.0.0.1/30": {}}}}
        old = self._iface("ens34", {"0": unit_cfg, "100": {"vlan_id": 100}})
        applier.apply(old, {})
        kernel.clear_nos_addresses.assert_called_once_with("ens34", unit_cfg)
        kernel.delete_interface.assert_any_call("ens34.100")
        kernel.delete_interface.assert_any_call("ens34")

    def test_interface_deleted_no_unit_0_does_not_call_sync(self):
        applier, kernel, _, _ = _make_applier()
        old = self._iface("ens34", {"100": {"vlan_id": 100}})
        applier.apply(old, {})
        kernel.sync_interface_addresses.assert_not_called()
        kernel.delete_interface.assert_any_call("ens34.100")
        kernel.delete_interface.assert_any_call("ens34")

    def test_top_level_apply_interface_still_called_on_change(self):
        applier, kernel, _, _ = _make_applier()
        old = {"interfaces": {"ens34": {"description": "old", "unit": {}}}}
        new = {"interfaces": {"ens34": {"description": "new", "unit": {}}}}
        applier.apply(old, new)
        kernel.apply_interface.assert_called_once_with("ens34", {"description": "new", "unit": {}})


# ---------------------------------------------------------------------------
# VLANs section
# vlans is a VLAN database (name→vlan_id). Bridges are NOT created here.
# Only IRB/SVI interfaces are managed when l3_interface is set.
# ---------------------------------------------------------------------------

class TestVlans:
    def test_new_vlan_without_l3_interface_does_nothing(self):
        applier, kernel, _, _ = _make_applier()
        applier.apply({}, {"vlans": {"vlan100": _VLAN_CFG}})
        kernel.apply_bridge.assert_not_called()
        kernel.apply_svi.assert_not_called()
        kernel.delete_interface.assert_not_called()

    def test_new_vlan_with_l3_interface_calls_apply_svi(self):
        applier, kernel, _, _ = _make_applier()
        applier.apply({}, {"vlans": {"vlan101": _VLAN_SVI_CFG}})
        kernel.apply_svi.assert_called_once_with("irb.101", {"vlan_id": 101})

    def test_removed_vlan_without_l3_interface_does_nothing(self):
        applier, kernel, _, _ = _make_applier()
        applier.apply({"vlans": {"vlan100": _VLAN_CFG}}, {})
        kernel.delete_interface.assert_not_called()

    def test_removed_vlan_with_l3_interface_deletes_svi(self):
        applier, kernel, _, _ = _make_applier()
        applier.apply({"vlans": {"vlan101": _VLAN_SVI_CFG}}, {})
        kernel.delete_interface.assert_called_once_with("irb.101")

    def test_unchanged_vlan_with_l3_interface_still_applies_svi(self):
        """apply_svi is always called so IRB address changes are never missed."""
        applier, kernel, _, _ = _make_applier()
        config = {"vlans": {"vlan101": _VLAN_SVI_CFG}}
        applier.apply(config, config)
        kernel.apply_svi.assert_called_once_with("irb.101", {"vlan_id": 101})

    def test_changed_vlan_with_l3_interface_calls_apply_svi(self):
        applier, kernel, _, _ = _make_applier()
        old = {"vlans": {"vlan101": {"vlan_id": 101, "l3_interface": "irb.101"}}}
        new = {"vlans": {"vlan101": {"vlan_id": 101, "l3_interface": "irb.101", "description": "mgmt"}}}
        applier.apply(old, new)
        kernel.apply_svi.assert_called_once_with("irb.101", {"vlan_id": 101})

    def test_svi_receives_irb_unit_addresses(self):
        """family_inet/family_inet6 from interfaces.irb.unit are merged into apply_svi call."""
        applier, kernel, _, _ = _make_applier()
        full_config = {
            "vlans": {"vlan101": {"vlan_id": 101, "l3_interface": "irb.101"}},
            "interfaces": {
                "irb": {
                    "unit": {
                        "101": {"family_inet": {"address": {"10.0.101.1/24": {}}}}
                    }
                }
            },
        }
        applier.apply({}, full_config)
        kernel.apply_svi.assert_called_once_with(
            "irb.101",
            {
                "vlan_id": 101,
                "family_inet": {"address": {"10.0.101.1/24": {}}},
            },
        )

    def test_changed_vlan_without_l3_interface_does_nothing(self):
        applier, kernel, _, _ = _make_applier()
        old = {"vlans": {"vlan100": {"vlan_id": 100}}}
        new = {"vlans": {"vlan100": {"vlan_id": 100, "description": "updated"}}}
        applier.apply(old, new)
        kernel.apply_svi.assert_not_called()
        kernel.apply_bridge.assert_not_called()


# ---------------------------------------------------------------------------
# Routing options / static routes section
# ---------------------------------------------------------------------------

class TestRoutingOptions:
    def _routing(self, prefix: str, route_cfg: dict) -> dict:
        return {"routing_options": {"static": {"route": {prefix: route_cfg}}}}

    def test_new_route_calls_apply_route(self):
        applier, kernel, _, _ = _make_applier()
        applier.apply({}, self._routing("10.0.0.0/24", _ROUTE_CFG))
        kernel.apply_route.assert_called_once_with("10.0.0.0/24", _ROUTE_CFG)

    def test_removed_route_calls_delete_route(self):
        applier, kernel, _, _ = _make_applier()
        applier.apply(self._routing("10.0.0.0/24", _ROUTE_CFG), {})
        kernel.delete_route.assert_called_once_with("10.0.0.0/24")

    def test_unchanged_route_not_reapplied(self):
        applier, kernel, _, _ = _make_applier()
        config = self._routing("10.0.0.0/24", _ROUTE_CFG)
        applier.apply(config, config)
        kernel.apply_route.assert_not_called()
        kernel.delete_route.assert_not_called()

    def test_new_route_calls_pfe_route_add_when_available(self):
        applier, kernel, _, pfe = _make_applier(pfe_available=True)
        applier.apply({}, self._routing("10.0.0.0/24", _ROUTE_CFG))
        pfe.fib.route_add.assert_called_once_with("10.0.0.0/24", "10.0.0.1", ifindex=0)

    def test_removed_route_calls_pfe_route_del_when_available(self):
        applier, kernel, _, pfe = _make_applier(pfe_available=True)
        applier.apply(self._routing("10.0.0.0/24", _ROUTE_CFG), {})
        pfe.fib.route_del.assert_called_once_with("10.0.0.0/24")

    def test_pfe_not_called_when_unavailable(self):
        applier, _, _, pfe = _make_applier(pfe_available=False)
        applier.apply({}, self._routing("10.0.0.0/24", _ROUTE_CFG))
        pfe.fib.route_add.assert_not_called()

    def test_pfe_route_add_failure_does_not_raise(self):
        applier, kernel, _, pfe = _make_applier(pfe_available=True)
        pfe.fib.route_add.side_effect = Exception("PFE down")
        # Should not raise; kernel apply still happens
        applier.apply({}, self._routing("10.0.0.0/24", _ROUTE_CFG))
        kernel.apply_route.assert_called_once()

    def test_pfe_route_del_failure_does_not_raise(self):
        applier, kernel, _, pfe = _make_applier(pfe_available=True)
        pfe.fib.route_del.side_effect = Exception("PFE down")
        applier.apply(self._routing("10.0.0.0/24", _ROUTE_CFG), {})
        kernel.delete_route.assert_called_once()

    def test_route_with_no_nexthop_passes_none_to_pfe(self):
        applier, _, _, pfe = _make_applier(pfe_available=True)
        applier.apply({}, self._routing("10.0.0.0/24", {"discard": True}))
        pfe.fib.route_add.assert_called_once_with("10.0.0.0/24", None, ifindex=0)


# ---------------------------------------------------------------------------
# Protocols section (IS-IS / BGP)
# ---------------------------------------------------------------------------

class TestProtocols:
    def _proto(self, isis=None, bgp=None) -> dict:
        p: dict = {}
        if isis is not None:
            p["isis"] = isis
        if bgp is not None:
            p["bgp"] = bgp
        return {"protocols": p}

    def test_isis_change_writes_frr_conf(self):
        applier, _, frr, _ = _make_applier()
        old = self._proto(isis={"interface": {}})
        new = self._proto(isis={"interface": {"eth0": {"point_to_point": True}}})
        with patch.object(applier._renderer, "render", return_value="rendered") as mock_render:
            applier.apply(old, new)
        frr.write_frr_conf.assert_called_once_with("rendered")

    def test_bgp_change_writes_frr_conf(self):
        applier, _, frr, _ = _make_applier()
        old = self._proto(bgp={"group": {}})
        new = self._proto(bgp={"group": {"IBGP": {"group_type": "internal"}}})
        with patch.object(applier._renderer, "render", return_value="rendered"):
            applier.apply(old, new)
        frr.write_frr_conf.assert_called_once()

    def test_no_protocol_change_skips_frr(self):
        applier, _, frr, _ = _make_applier()
        config = self._proto(isis={"interface": {"eth0": {}}})
        applier.apply(config, config)
        frr.write_frr_conf.assert_not_called()

    def test_renderer_receives_full_new_config(self):
        applier, _, frr, _ = _make_applier()
        new_config = {
            "protocols": {"isis": {"interface": {"eth0": {}}}},
            "routing_options": {"router_id": "1.1.1.1"},
        }
        with patch.object(applier._renderer, "render", return_value="x") as mock_render:
            applier.apply({}, new_config)
        mock_render.assert_called_once_with(new_config)

    def test_isis_added_triggers_frr_write(self):
        applier, _, frr, _ = _make_applier()
        with patch.object(applier._renderer, "render", return_value="conf"):
            applier.apply({}, self._proto(isis={"interface": {}}))
        frr.write_frr_conf.assert_called_once_with("conf")

    def test_isis_removed_triggers_frr_write(self):
        applier, _, frr, _ = _make_applier()
        with patch.object(applier._renderer, "render", return_value="conf"):
            applier.apply(self._proto(isis={"interface": {}}), {})
        frr.write_frr_conf.assert_called_once_with("conf")


# ---------------------------------------------------------------------------
# Section isolation — failures
# ---------------------------------------------------------------------------

class TestSectionIsolation:
    def test_interface_failure_does_not_stop_vlans(self):
        applier, kernel, _, _ = _make_applier()
        kernel.apply_interface.side_effect = RuntimeError("kernel exploded")
        new = {
            "interfaces": {"eth0": _IFACE_CFG},
            "vlans": {"vlan101": _VLAN_SVI_CFG},
        }
        applier.apply({}, new)  # must not raise
        kernel.apply_svi.assert_called_once_with("irb.101", {"vlan_id": 101})

    def test_all_sections_fail_raises_config_apply_error(self):
        applier, kernel, frr, pfe = _make_applier()
        kernel.apply_interface.side_effect = RuntimeError("fail")
        kernel.apply_svi.side_effect = RuntimeError("fail")
        kernel.apply_route.side_effect = RuntimeError("fail")
        frr.write_frr_conf.side_effect = RuntimeError("fail")

        new = {
            "interfaces": {"eth0": _IFACE_CFG},
            "vlans": {"vlan101": _VLAN_SVI_CFG},
            "routing_options": {"static": {"route": {"10.0.0.0/24": _ROUTE_CFG}}},
            "protocols": {"isis": {"interface": {"eth0": {}}}},
        }
        with pytest.raises(ConfigApplyError):
            applier.apply({}, new)

    def test_three_sections_fail_no_error_raised(self):
        """One passing section is enough to suppress ConfigApplyError."""
        applier, kernel, frr, _ = _make_applier()
        kernel.apply_interface.side_effect = RuntimeError("fail")
        kernel.apply_svi.side_effect = RuntimeError("fail")
        frr.write_frr_conf.side_effect = RuntimeError("fail")

        new = {
            "interfaces": {"eth0": _IFACE_CFG},
            "vlans": {"vlan101": _VLAN_SVI_CFG},
            "routing_options": {"static": {"route": {"10.0.0.0/24": _ROUTE_CFG}}},
            "protocols": {"isis": {"interface": {"eth0": {}}}},
        }
        with patch.object(applier._renderer, "render", return_value="x"):
            applier.apply({}, new)  # routing_options succeeds → no ConfigApplyError

    def test_empty_old_and_new_config_is_noop(self):
        applier, kernel, frr, pfe = _make_applier()
        applier.apply({}, {})
        kernel.apply_interface.assert_not_called()
        kernel.delete_interface.assert_not_called()
        kernel.apply_bridge.assert_not_called()
        kernel.apply_route.assert_not_called()
        kernel.delete_route.assert_not_called()
        frr.write_frr_conf.assert_not_called()
        pfe.fib.route_add.assert_not_called()


# ---------------------------------------------------------------------------
# Switchport (family_ethernet_switching) handling
# ---------------------------------------------------------------------------

class TestSwitchport:
    def _sw_iface(self, mode: str, members: list, vlans: dict = None) -> dict:
        new_cfg = {
            "interfaces": {
                "eth1": {
                    "unit": {
                        "0": {
                            "family_ethernet_switching": {
                                "interface_mode": mode,
                                "vlan": {"members": members},
                            }
                        }
                    }
                }
            }
        }
        if vlans is not None:
            new_cfg["vlans"] = vlans
        return new_cfg

    def test_new_switchport_calls_apply_bridge(self):
        applier, kernel, _, _ = _make_applier()
        cfg = self._sw_iface("access", ["vlan100"], {"vlan100": {"vlan_id": 100}})
        applier.apply({}, cfg)
        kernel.apply_bridge.assert_any_call("nos-br", {})

    def test_new_switchport_resolves_vlan_name_to_id(self):
        applier, kernel, _, _ = _make_applier()
        cfg = self._sw_iface("access", ["vlan100"], {"vlan100": {"vlan_id": 100}})
        applier.apply({}, cfg)
        kernel.apply_vlan.assert_called_with(
            "nos-br", "eth1", {"interface_mode": "access", "vlans": [100]}
        )

    def test_new_switchport_with_numeric_string_member(self):
        applier, kernel, _, _ = _make_applier()
        cfg = self._sw_iface("access", ["101"])
        applier.apply({}, cfg)
        kernel.apply_vlan.assert_called_with(
            "nos-br", "eth1", {"interface_mode": "access", "vlans": [101]}
        )

    def test_new_switchport_with_integer_member(self):
        applier, kernel, _, _ = _make_applier()
        cfg = self._sw_iface("trunk", [100, 200])
        applier.apply({}, cfg)
        kernel.apply_vlan.assert_called_with(
            "nos-br", "eth1", {"interface_mode": "trunk", "vlans": [100, 200]}
        )

    def test_new_switchport_with_all_member(self):
        applier, kernel, _, _ = _make_applier()
        cfg = self._sw_iface("trunk", ["all"])
        applier.apply({}, cfg)
        kernel.apply_vlan.assert_called_with(
            "nos-br", "eth1", {"interface_mode": "trunk", "vlans": ["all"]}
        )

    def test_unchanged_switchport_not_reapplied(self):
        applier, kernel, _, _ = _make_applier()
        cfg = self._sw_iface("access", ["vlan100"], {"vlan100": {"vlan_id": 100}})
        applier.apply(cfg, cfg)
        kernel.apply_bridge.assert_not_called()
        kernel.apply_vlan.assert_not_called()

    def test_removed_switchport_unit_detaches_from_bridge(self):
        applier, kernel, _, _ = _make_applier()
        old = self._sw_iface("access", ["vlan100"], {"vlan100": {"vlan_id": 100}})
        new = {"interfaces": {"eth1": {}}}
        applier.apply(old, new)
        kernel.detach_port.assert_called_with("nos-br", "eth1")

    def test_removed_interface_with_switchport_calls_delete(self):
        applier, kernel, _, _ = _make_applier()
        old = self._sw_iface("access", ["vlan100"], {"vlan100": {"vlan_id": 100}})
        applier.apply(old, {})
        kernel.delete_interface.assert_any_call("eth1")

    def test_vlan_name_not_in_vlans_config_produces_empty_vlan_ids(self):
        """If the referenced VLAN name doesn't exist in vlans, vlans list is empty."""
        applier, kernel, _, _ = _make_applier()
        cfg = self._sw_iface("access", ["ghost_vlan"])
        applier.apply({}, cfg)
        kernel.apply_vlan.assert_called_with(
            "nos-br", "eth1", {"interface_mode": "access", "vlans": []}
        )

    def test_bare_integer_member_not_wrapped_in_list(self):
        """Config written before schema coercion may store members as a bare int."""
        applier, kernel, _, _ = _make_applier()
        cfg = {
            "interfaces": {
                "ens34": {
                    "unit": {
                        "0": {
                            "family_ethernet_switching": {
                                "interface_mode": "access",
                                "vlan": {"members": 101},
                            }
                        }
                    }
                }
            }
        }
        applier.apply({}, cfg)
        kernel.apply_vlan.assert_called_with(
            "nos-br", "ens34", {"interface_mode": "access", "vlans": [101]}
        )


# ---------------------------------------------------------------------------
# vlan-id in unit config
# ---------------------------------------------------------------------------

class TestUnitVlanId:
    def test_unit_vlan_id_passed_to_apply_subinterface(self):
        """vlan_id from the unit config dict reaches apply_subinterface."""
        applier, kernel, _, _ = _make_applier()
        unit_cfg = {"vlan_id": 100, "family_inet": {"address": {"10.0.1.1/24": {}}}}
        cfg = {"interfaces": {"ens34": {"unit": {"100": unit_cfg}}}}
        applier.apply({}, cfg)
        kernel.apply_subinterface.assert_called_once_with("ens34", 100, unit_cfg)

    def test_unit_vlan_id_without_address(self):
        """A unit with only vlan-id (no family) still calls apply_subinterface."""
        applier, kernel, _, _ = _make_applier()
        unit_cfg = {"vlan_id": 200}
        cfg = {"interfaces": {"ens34": {"unit": {"200": unit_cfg}}}}
        applier.apply({}, cfg)
        kernel.apply_subinterface.assert_called_once_with("ens34", 200, unit_cfg)
