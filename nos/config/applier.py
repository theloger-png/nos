"""Config applier — translates committed config changes into driver and PFE calls."""
from __future__ import annotations

from typing import Any, Dict

from nos.drivers.base import BaseDriver
from nos.drivers.frr.renderer import FRRRenderer
from nos.pfe.manager import PFEManager
from nos.utils.logger import get_logger

log = get_logger(__name__)


class ConfigApplyError(Exception):
    """Raised when every config section fails to apply."""


class ConfigApplier:
    """Translates a config diff into kernel driver, FRR driver, and PFE calls.

    Each section is applied independently.  A failure in one section logs an
    error but continues with the remaining sections.  ConfigApplyError is only
    raised when *all* sections fail.
    """

    def __init__(
        self,
        kernel_driver: BaseDriver,
        frr_driver: Any,  # expected: FRRClient — must have write_frr_conf(str)
        pfe_manager: PFEManager,
    ) -> None:
        self._kernel = kernel_driver
        self._frr = frr_driver
        self._pfe = pfe_manager
        self._renderer = FRRRenderer()

    def apply(self, old_config: Dict[str, Any], new_config: Dict[str, Any]) -> None:
        """Apply the diff between *old_config* and *new_config* to the system.

        Raises ConfigApplyError if every section fails.
        """
        handlers = {
            "interfaces": self._apply_interfaces,
            "vlans": self._apply_vlans,
            "routing_options": self._apply_routing_options,
            "protocols": self._apply_protocols,
        }

        failed: list[str] = []
        for section, handler in handlers.items():
            old_section = old_config.get(section) or {}
            new_section = new_config.get(section) or {}
            try:
                handler(old_section, new_section, new_config)
            except Exception as exc:
                log.error("Section %r failed to apply: %s", section, exc)
                failed.append(section)

        if len(failed) == len(handlers):
            raise ConfigApplyError(f"All config sections failed: {failed}")

    # ── section handlers ─────────────────────────────────────────────────────

    def _resolve_vlan_ids(
        self, members: list, vlans_config: Dict[str, Any]
    ) -> list:
        ids = []
        for m in members:
            if m == "all":
                return ["all"]
            if isinstance(m, int):
                ids.append(m)
            elif isinstance(m, str):
                if m.isdigit():
                    ids.append(int(m))
                else:
                    vlan_id = (vlans_config.get(m) or {}).get("vlan_id")
                    if vlan_id is not None:
                        ids.append(vlan_id)
        return ids

    def _apply_interfaces(
        self,
        old: Dict[str, Any],
        new: Dict[str, Any],
        full_config: Dict[str, Any],
    ) -> None:
        for name in set(old) - set(new):
            old_cfg = old.get(name) or {}
            old_units = old_cfg.get("unit") or {}
            if "0" in old_units:
                log.info("Clearing addresses on interface %s (deleted)", name)
                self._kernel.clear_nos_addresses(name, old_units.get("0") or {})
            for unit_num_str in old_units:
                unit_num = int(unit_num_str)
                if unit_num > 0:
                    log.info("Deleting subinterface %s.%d", name, unit_num)
                    self._kernel.delete_interface(f"{name}.{unit_num}")
            if any((unit_cfg or {}).get("family_ethernet_switching") for unit_cfg in old_units.values()):
                log.info("Detaching %s from bridge (interface deleted)", name)
                self._kernel.detach_port("nos-br", name)
                if not self._kernel.get_bridge_ports("nos-br"):
                    log.info("No ports left on nos-br; deleting bridge")
                    self._kernel.delete_bridge("nos-br")
            log.info("Deleting interface %s", name)
            self._kernel.delete_interface(name)

        for name, config in new.items():
            cfg = config or {}
            old_cfg = old.get(name) or {}

            log.info("Applying interface %s", name)
            self._kernel.apply_interface(name, cfg)

            old_units = old_cfg.get("unit") or {}
            new_units = cfg.get("unit") or {}

            for unit_num_str in set(old_units) - set(new_units):
                unit_num = int(unit_num_str)
                old_unit_cfg = old_units.get(unit_num_str) or {}
                if unit_num == 0:
                    log.info("Clearing addresses on interface %s (unit 0 removed)", name)
                    self._kernel.sync_interface_addresses(name, {})
                else:
                    log.info("Deleting subinterface %s.%d", name, unit_num)
                    self._kernel.delete_interface(f"{name}.{unit_num}")
                if old_unit_cfg.get("family_ethernet_switching"):
                    log.info("Detaching %s from bridge (unit removed)", name)
                    self._kernel.detach_port("nos-br", name)
                    if not self._kernel.get_bridge_ports("nos-br"):
                        log.info("No ports left on nos-br; deleting bridge")
                        self._kernel.delete_bridge("nos-br")

            for unit_num_str, unit_cfg in new_units.items():
                unit_num = int(unit_num_str)
                unit_config = unit_cfg or {}
                if unit_config == (old_units.get(unit_num_str) or {}):
                    continue
                if unit_num == 0:
                    log.info("Syncing addresses on interface %s (unit 0)", name)
                    self._kernel.sync_interface_addresses(name, unit_config)
                else:
                    log.info("Applying subinterface %s.%d", name, unit_num)
                    self._kernel.apply_subinterface(name, unit_num, unit_config)
                sw = unit_config.get("family_ethernet_switching")
                if sw:
                    log.info("Applying switchport on %s", name)
                    self._kernel.apply_bridge("nos-br", {})
                    vlans_cfg = full_config.get("vlans") or {}
                    members = (sw.get("vlan") or {}).get("members") or []
                    if not isinstance(members, list):
                        members = [members]
                    vlan_ids = self._resolve_vlan_ids(members, vlans_cfg)
                    self._kernel.apply_vlan("nos-br", name, {
                        "interface_mode": sw.get("interface_mode"),
                        "vlans": vlan_ids,
                    })

    def _apply_vlans(
        self,
        old: Dict[str, Any],
        new: Dict[str, Any],
        full_config: Dict[str, Any],
    ) -> None:
        # vlans section is a VLAN database (name→vlan_id mapping).
        # The single 'nos-br' bridge is managed by _apply_interfaces.
        # Here we only handle IRB/SVI interfaces when l3_interface is set.
        for name in set(old) - set(new):
            old_cfg = old.get(name) or {}
            svi_name = old_cfg.get("l3_interface")
            if svi_name:
                log.info("Deleting SVI %s (VLAN %s removed)", svi_name, name)
                self._kernel.delete_interface(svi_name)

        for name, config in new.items():
            cfg = config or {}
            svi_name = cfg.get("l3_interface")
            if svi_name:
                log.info("Applying SVI %s for VLAN %s", svi_name, name)
                vlan_id = cfg.get("vlan_id")
                irb_units = (full_config.get("interfaces") or {}).get("irb", {}).get("unit") or {}
                unit_cfg = irb_units.get(str(vlan_id)) or {}
                self._kernel.apply_svi(svi_name, {"vlan_id": vlan_id, **unit_cfg})

    def _apply_routing_options(
        self,
        old: Dict[str, Any],
        new: Dict[str, Any],
        full_config: Dict[str, Any],
    ) -> None:
        old_routes = (old.get("static") or {}).get("route") or {}
        new_routes = (new.get("static") or {}).get("route") or {}

        for prefix in set(old_routes) - set(new_routes):
            log.info("Deleting static route %s", prefix)
            self._kernel.delete_route(prefix)
            if self._pfe.is_available():
                try:
                    self._pfe.fib.route_del(prefix)
                except Exception as exc:
                    log.error("PFE route_del failed for %s: %s", prefix, exc)

        for prefix, config in new_routes.items():
            if config != old_routes.get(prefix):
                cfg = config or {}
                log.info("Applying static route %s", prefix)
                self._kernel.apply_route(prefix, cfg)
                if self._pfe.is_available():
                    try:
                        self._pfe.fib.route_add(prefix, cfg.get("next_hop"), ifindex=0)
                    except Exception as exc:
                        log.error("PFE route_add failed for %s: %s", prefix, exc)

    def _apply_protocols(
        self,
        old: Dict[str, Any],
        new: Dict[str, Any],
        full_config: Dict[str, Any],
    ) -> None:
        isis_changed = new.get("isis") != old.get("isis")
        bgp_changed = new.get("bgp") != old.get("bgp")
        if not isis_changed and not bgp_changed:
            return

        log.info("Applying protocol config (IS-IS/BGP changed)")
        rendered = self._renderer.render(full_config)
        self._frr.write_frr_conf(rendered)
