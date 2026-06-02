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
                self._kernel.sync_interface_addresses(name, {})
            for unit_num_str in old_units:
                unit_num = int(unit_num_str)
                if unit_num > 0:
                    log.info("Deleting subinterface %s.%d", name, unit_num)
                    self._kernel.delete_interface(f"{name}.{unit_num}")
            log.info("Deleting interface %s", name)
            self._kernel.delete_interface(name)

        for name, config in new.items():
            cfg = config or {}
            old_cfg = old.get(name) or {}

            if cfg != old_cfg:
                log.info("Applying interface %s", name)
                self._kernel.apply_interface(name, cfg)

            old_units = old_cfg.get("unit") or {}
            new_units = cfg.get("unit") or {}

            for unit_num_str in set(old_units) - set(new_units):
                unit_num = int(unit_num_str)
                if unit_num == 0:
                    log.info("Clearing addresses on interface %s (unit 0 removed)", name)
                    self._kernel.sync_interface_addresses(name, {})
                else:
                    log.info("Deleting subinterface %s.%d", name, unit_num)
                    self._kernel.delete_interface(f"{name}.{unit_num}")

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

    def _apply_vlans(
        self,
        old: Dict[str, Any],
        new: Dict[str, Any],
        full_config: Dict[str, Any],
    ) -> None:
        for name in set(old) - set(new):
            log.info("Deleting VLAN %s", name)
            self._kernel.delete_interface(name)

        for name, config in new.items():
            if config != old.get(name):
                cfg = config or {}
                log.info("Applying bridge for VLAN %s", name)
                self._kernel.apply_bridge(name, cfg)
                for port in cfg.get("members") or []:
                    self._kernel.apply_vlan(name, port, cfg)

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
