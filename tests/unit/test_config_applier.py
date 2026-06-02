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

    def test_unchanged_interface_not_reapplied(self):
        applier, kernel, _, _ = _make_applier()
        config = {"interfaces": {"eth0": _IFACE_CFG}}
        applier.apply(config, config)
        kernel.apply_interface.assert_not_called()
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
        kernel.apply_interface.assert_called_once_with("eth1", {"description": "b"})

    def test_none_config_treated_as_empty_dict(self):
        applier, kernel, _, _ = _make_applier()
        applier.apply({"interfaces": {"eth0": None}}, {"interfaces": {"eth0": None}})
        kernel.apply_interface.assert_not_called()


# ---------------------------------------------------------------------------
# VLANs section
# ---------------------------------------------------------------------------

class TestVlans:
    def test_new_vlan_calls_apply_bridge(self):
        applier, kernel, _, _ = _make_applier()
        applier.apply({}, {"vlans": {"vlan100": _VLAN_CFG}})
        kernel.apply_bridge.assert_called_once_with("vlan100", _VLAN_CFG)

    def test_removed_vlan_calls_delete_interface(self):
        applier, kernel, _, _ = _make_applier()
        applier.apply({"vlans": {"vlan100": _VLAN_CFG}}, {})
        kernel.delete_interface.assert_called_once_with("vlan100")

    def test_unchanged_vlan_not_reapplied(self):
        applier, kernel, _, _ = _make_applier()
        config = {"vlans": {"vlan100": _VLAN_CFG}}
        applier.apply(config, config)
        kernel.apply_bridge.assert_not_called()

    def test_changed_vlan_calls_apply_bridge(self):
        applier, kernel, _, _ = _make_applier()
        old = {"vlans": {"vlan100": {"vlan_id": 100}}}
        new = {"vlans": {"vlan100": {"vlan_id": 100, "description": "updated"}}}
        applier.apply(old, new)
        kernel.apply_bridge.assert_called_once_with(
            "vlan100", {"vlan_id": 100, "description": "updated"}
        )

    def test_vlan_with_members_calls_apply_vlan(self):
        applier, kernel, _, _ = _make_applier()
        vlan_cfg = {"vlan_id": 100, "members": ["eth1", "eth2"]}
        applier.apply({}, {"vlans": {"vlan100": vlan_cfg}})
        assert kernel.apply_vlan.call_count == 2
        kernel.apply_vlan.assert_any_call("vlan100", "eth1", vlan_cfg)
        kernel.apply_vlan.assert_any_call("vlan100", "eth2", vlan_cfg)

    def test_vlan_without_members_does_not_call_apply_vlan(self):
        applier, kernel, _, _ = _make_applier()
        applier.apply({}, {"vlans": {"vlan100": _VLAN_CFG}})
        kernel.apply_vlan.assert_not_called()


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
            "vlans": {"vlan100": _VLAN_CFG},
        }
        applier.apply({}, new)  # must not raise
        kernel.apply_bridge.assert_called_once()

    def test_all_sections_fail_raises_config_apply_error(self):
        applier, kernel, frr, pfe = _make_applier()
        kernel.apply_interface.side_effect = RuntimeError("fail")
        kernel.apply_bridge.side_effect = RuntimeError("fail")
        kernel.apply_route.side_effect = RuntimeError("fail")
        frr.write_frr_conf.side_effect = RuntimeError("fail")

        new = {
            "interfaces": {"eth0": _IFACE_CFG},
            "vlans": {"vlan100": _VLAN_CFG},
            "routing_options": {"static": {"route": {"10.0.0.0/24": _ROUTE_CFG}}},
            "protocols": {"isis": {"interface": {"eth0": {}}}},
        }
        with pytest.raises(ConfigApplyError):
            applier.apply({}, new)

    def test_three_sections_fail_no_error_raised(self):
        """One passing section is enough to suppress ConfigApplyError."""
        applier, kernel, frr, _ = _make_applier()
        kernel.apply_interface.side_effect = RuntimeError("fail")
        kernel.apply_bridge.side_effect = RuntimeError("fail")
        frr.write_frr_conf.side_effect = RuntimeError("fail")

        new = {
            "interfaces": {"eth0": _IFACE_CFG},
            "vlans": {"vlan100": _VLAN_CFG},
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
