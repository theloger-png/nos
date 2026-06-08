"""Config applier — translates committed config changes into driver and PFE calls."""
from __future__ import annotations

import copy
import os
from typing import TYPE_CHECKING, Any, Dict, Optional

from pyroute2 import IPRoute

from nos.drivers.base import BaseDriver
from nos.drivers.frr.renderer import FRRRenderer
from nos.pfe.manager import PFEManager
from nos.utils.logger import get_logger

if TYPE_CHECKING:
    from nos.config.store import ConfigStore

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
        store: Optional["ConfigStore"] = None,
        dhcp_driver: Optional[Any] = None,  # DnsmasqDriver or compatible
    ) -> None:
        self._kernel = kernel_driver
        self._frr = frr_driver
        self._pfe = pfe_manager
        self._store = store
        self._dhcp = dhcp_driver
        self._renderer = FRRRenderer()
        self._pending_reverse_alias_map: Optional[dict] = None

    def apply(self, old_config: Dict[str, Any], new_config: Dict[str, Any]) -> None:
        """Apply the diff between *old_config* and *new_config* to the system.

        Raises ConfigApplyError if every section fails.
        """
        self._pending_reverse_alias_map = None
        handlers = {
            "system": self._apply_system,
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

        if self._dhcp is not None:
            try:
                self._dhcp.apply(new_config)
            except Exception as exc:
                log.error("DHCP server driver failed: %s", exc)
            try:
                self._dhcp.apply_client(new_config)
            except Exception as exc:
                log.error("DHCP client driver failed: %s", exc)

        self._apply_nat(old_config, new_config)

    # ── section handlers ─────────────────────────────────────────────────────

    def _apply_system(
        self,
        old: Dict[str, Any],
        new: Dict[str, Any],
        full_config: Dict[str, Any],
    ) -> None:
        old_login = (old or {}).get("login") or {}
        new_login = (new or {}).get("login") or {}
        if old_login != new_login:
            from nos.drivers.kernel.users import UserDriver
            try:
                UserDriver().apply(new_login)
            except Exception as exc:
                log.error("UserDriver failed: %s", exc)

        old_services = (old or {}).get("services") or {}
        new_services = (new or {}).get("services") or {}
        old_ssh = old_services.get("ssh") or {}
        new_ssh = new_services.get("ssh") or {}
        if old_ssh != new_ssh:
            from nos.drivers.kernel.ssh import SshDriver
            try:
                if new_ssh:
                    SshDriver().apply(
                        port=new_ssh.get("port", 22),
                        protocol_version=new_ssh.get("protocol_version", "v2"),
                        root_login=new_ssh.get("root_login", "deny"),
                    )
            except Exception as exc:
                log.error("SshDriver failed: %s", exc)

        old_rename = (old or {}).get("interface_rename", False)
        new_rename = (new or {}).get("interface_rename", False)
        if old_rename == new_rename:
            return

        from nos.utils.interface_alias import (
            detect_physical_interfaces,
            generate_alias_map,
            load_alias_map,
            migrate_config,
            migrate_config_reverse,
            save_alias_map,
        )

        if new_rename:
            # False → True: detect physical interfaces, build and save alias map,
            # then migrate the running and candidate configs to use aliases.
            physical = detect_physical_interfaces()
            alias_map = generate_alias_map(physical)
            try:
                save_alias_map(alias_map)
            except OSError as exc:
                log.warning(
                    "interface_rename: could not persist alias map to %s: %s",
                    "/opt/nos/interface_map.json",
                    exc,
                )
            if self._store is not None:
                migrated = migrate_config(self._store.running, alias_map)
                self._store.running = migrated
                self._store.save_running()
                self._store.candidate = copy.deepcopy(migrated)
                self._store.save_candidate()
            log.info(
                "interface_rename enabled: %d aliases generated", len(alias_map)
            )
        else:
            # True → False: reverse-migrate configs back to physical names,
            # then delete the alias map file.
            alias_map = load_alias_map()
            # Stash before deleting the file so _apply_interfaces can still
            # translate alias names (et0→ens33) in new_config, which still
            # holds alias names because _apply_system replaced store.running
            # with a new object rather than mutating it in place.
            self._pending_reverse_alias_map = alias_map
            if alias_map is not None and self._store is not None:
                migrated = migrate_config_reverse(self._store.running, alias_map)
                self._store.running = migrated
                self._store.save_running()
                self._store.candidate = copy.deepcopy(migrated)
                self._store.save_candidate()
            try:
                os.remove("/opt/nos/interface_map.json")
            except FileNotFoundError:
                pass
            log.info("interface_rename disabled: physical names restored")

    # ── alias translation helpers ─────────────────────────────────────────────

    def _get_alias_map(self, full_config: Dict[str, Any]) -> Optional[dict]:
        """Return alias→physical map for use in kernel/FRR calls.

        Enabled (rename=True): load from the map file.
        Disable transition (rename=False, pending map set by _apply_system):
            return the in-memory map so handlers can still translate alias names
            that survive in new_config after _apply_system replaced store.running.
        Steady-state disabled: return None (no translation needed).
        """
        if (full_config.get("system") or {}).get("interface_rename", False):
            from nos.utils.interface_alias import load_alias_map
            return load_alias_map()
        return self._pending_reverse_alias_map

    @staticmethod
    def _phys(name: str, alias_map: Optional[dict]) -> str:
        """Translate an alias interface name to its physical kernel name."""
        if alias_map is None:
            return name
        from nos.utils.interface_alias import to_physical
        return to_physical(name, alias_map)

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
        alias_map = self._get_alias_map(full_config)

        for name in set(old) - set(new):
            phys = self._phys(name, alias_map)
            old_cfg = old.get(name) or {}
            old_units = old_cfg.get("unit") or {}
            if "0" in old_units:
                log.info("Clearing addresses on interface %s (deleted)", name)
                self._kernel.clear_nos_addresses(phys, old_units.get("0") or {})
            for unit_num_str in old_units:
                unit_num = int(unit_num_str)
                if unit_num > 0:
                    log.info("Deleting subinterface %s.%d", name, unit_num)
                    self._kernel.delete_interface(f"{phys}.{unit_num}")
            if any((unit_cfg or {}).get("family_ethernet_switching") for unit_cfg in old_units.values()):
                log.info("Detaching %s from bridge (interface deleted)", name)
                self._kernel.detach_port("nos-br", phys)
                if not self._kernel.get_bridge_ports("nos-br"):
                    log.info("No ports left on nos-br; deleting bridge")
                    self._kernel.delete_bridge("nos-br")
            log.info("Deleting interface %s", name)
            self._kernel.delete_interface(phys)

        for name, config in new.items():
            phys = self._phys(name, alias_map)
            cfg = config or {}
            old_cfg = old.get(name) or {}

            log.info("Applying interface %s", name)
            self._kernel.apply_interface(phys, cfg)

            old_units = old_cfg.get("unit") or {}
            new_units = cfg.get("unit") or {}

            for unit_num_str in set(old_units) - set(new_units):
                unit_num = int(unit_num_str)
                old_unit_cfg = old_units.get(unit_num_str) or {}
                if unit_num == 0:
                    log.info("Clearing addresses on interface %s (unit 0 removed)", name)
                    self._kernel.sync_interface_addresses(phys, {})
                else:
                    log.info("Deleting subinterface %s.%d", name, unit_num)
                    self._kernel.delete_interface(f"{phys}.{unit_num}")
                if old_unit_cfg.get("family_ethernet_switching"):
                    log.info("Detaching %s from bridge (unit removed)", name)
                    self._kernel.detach_port("nos-br", phys)
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
                    self._kernel.sync_interface_addresses(phys, unit_config)
                else:
                    log.info("Applying subinterface %s.%d", name, unit_num)
                    self._kernel.apply_subinterface(phys, unit_num, unit_config)
                sw = unit_config.get("family_ethernet_switching")
                if sw:
                    log.info("Applying switchport on %s", name)
                    self._kernel.apply_bridge("nos-br", {"ports": [phys]})
                    vlans_cfg = full_config.get("vlans") or {}
                    members = (sw.get("vlan") or {}).get("members") or []
                    if not isinstance(members, list):
                        members = [members]
                    vlan_ids = self._resolve_vlan_ids(members, vlans_cfg)
                    self._kernel.apply_vlan("nos-br", phys, {
                        "interface_mode": sw.get("interface_mode"),
                        "vlans": vlan_ids,
                    })
                    iface_mode = sw.get("interface_mode")
                    if (self._pfe.is_available()
                            and iface_mode in ("access", "trunk")
                            and len(vlan_ids) == 1
                            and isinstance(vlan_ids[0], int)):
                        try:
                            with IPRoute() as ip:
                                idx = ip.link_lookup(ifname=phys)
                            if idx:
                                xdp_mode = 0 if iface_mode == "access" else 1
                                self._pfe.port_vlan_set(
                                    idx[0], vlan_ids[0], xdp_mode
                                )
                        except Exception as exc:
                            log.error(
                                "PFE port_vlan_set failed for %s: %s", name, exc
                            )

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
                if vlan_id is not None:
                    self._kernel.vlan_add_self("nos-br", vlan_id)

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

    def _apply_nat(
        self,
        old_config: Dict[str, Any],
        new_config: Dict[str, Any],
    ) -> None:
        new_nat = (new_config.get("security") or {}).get("nat") or {}
        if not new_nat:
            return

        from nos.config.schema import SecurityConfig
        from nos.drivers.kernel.nat import NatDriver

        log.info("Applying NAT config change")
        try:
            security = SecurityConfig.model_validate(
                new_config.get("security") or {}
            )
            alias_map = self._get_alias_map(new_config)

            def alias_to_kernel(name: str) -> str:
                return self._phys(name, alias_map)

            NatDriver().apply(security.nat, alias_to_kernel)
        except Exception as exc:
            log.error("NAT driver failed: %s", exc)

    def _apply_protocols(
        self,
        old: Dict[str, Any],
        new: Dict[str, Any],
        full_config: Dict[str, Any],
    ) -> None:
        isis_changed = new.get("isis") != old.get("isis")
        bgp_changed = new.get("bgp") != old.get("bgp")
        ospf_changed = new.get("ospf") != old.get("ospf")
        if not isis_changed and not bgp_changed and not ospf_changed:
            return

        log.info("Applying protocol config (IS-IS/BGP/OSPF changed)")
        alias_map = self._get_alias_map(full_config)
        render_config = full_config
        if alias_map is not None:
            from nos.utils.interface_alias import migrate_config_reverse
            render_config = migrate_config_reverse(full_config, alias_map)
        rendered = self._renderer.render(render_config)
        self._frr.write_frr_conf(rendered)

        active_protocols = {p for p in ("bgp", "isis", "ospf") if new.get(p)}
        self._frr.sync_daemons(active_protocols)
