from __future__ import annotations

import ipaddress
import json
import logging
import os
import re
from pathlib import Path
from typing import Any, Callable, Dict, Optional

from pyroute2 import IPRoute

logger = logging.getLogger(__name__)

# Physical / pre-existing interface name prefixes — skip creation for these.
# Note: plain 'lo' (system loopback) stays here; lo0, lo1, … are NOS dummies.
_PHYSICAL_PREFIXES = ("eth", "ens", "enp", "eno", "lo", "bond", "team")

# NOS-managed loopback dummy interfaces: lo0, lo1, lo2, …  (NOT plain 'lo').
_LOOPBACK_DUMMY_RE = re.compile(r"^lo\d+$")

_STATE_FILE = Path("/opt/nos/managed_addresses.json")


def _load_managed_addresses() -> dict[str, set[tuple[str, int]]]:
    try:
        raw = json.loads(_STATE_FILE.read_text())
        return {
            iface: {(str(addr), int(plen)) for addr, plen in entries}
            for iface, entries in raw.items()
        }
    except FileNotFoundError:
        return {}
    except Exception as exc:
        logger.warning("Could not load %s: %s; starting with empty address tracking", _STATE_FILE, exc)
        return {}


def _save_managed_addresses() -> None:
    try:
        os.makedirs(_STATE_FILE.parent, exist_ok=True)
        data = {
            iface: [list(entry) for entry in entries]
            for iface, entries in _nos_managed_addresses.items()
        }
        with open(_STATE_FILE, 'w') as f:
            f.write(json.dumps(data))
    except Exception as exc:
        logger.warning("Could not save managed addresses to %s: %s", _STATE_FILE, exc)


# Tracks which (address, prefixlen) tuples NOS has applied per interface name.
# Only addresses recorded here are candidates for removal; OS/DHCP addresses are left alone.
# Populated from disk on module load so tracking survives nos-cli restarts.
_nos_managed_addresses: dict[str, set[tuple[str, int]]] = _load_managed_addresses()


class InterfaceDriver:
    """Manages network interfaces via pyroute2 Netlink.

    ``iproute_factory`` can be injected in tests to replace IPRoute with a mock.
    """

    def __init__(self, iproute_factory: Optional[Callable] = None) -> None:
        self._iproute_factory: Callable = iproute_factory or IPRoute

    # ------------------------------------------------------------------
    # Public interface
    # ------------------------------------------------------------------

    def apply_interface(self, name: str, config: Dict[str, Any]) -> None:
        """Create or update a network interface.

        For physical interfaces the kernel already knows about them; we only
        configure attributes.  Virtual interfaces (dummy / veth / etc.) are
        created when they do not yet exist.
        """
        with self._iproute_factory() as ip:
            idx = self._lookup(ip, name)

            if idx is None:
                if self._is_physical(name):
                    logger.warning("Interface %s not found in kernel; skipping", name)
                    return
                self._create_dummy(ip, name)
                idx = self._lookup(ip, name)
                if idx is None:
                    raise RuntimeError(f"Failed to create interface {name}")

            self._apply_attrs(ip, idx, config)
            self._sync_addresses(ip, idx, name, config)
            self._apply_state(ip, idx, config)

    def clear_nos_addresses(self, name: str, old_config: Dict[str, Any]) -> None:
        """Remove only the IP addresses explicitly listed in old_config from the interface.

        Reads family_inet and family_inet6 address keys from old_config and deletes
        exactly those addresses from the kernel.  Addresses not in old_config are
        left untouched.
        """
        family_inet = (old_config.get("family_inet") or {})
        family_inet6 = (old_config.get("family_inet6") or {})

        to_remove: list[tuple[str, int]] = []
        for addr_str in (family_inet.get("address") or {}):
            net = ipaddress.ip_interface(addr_str)
            to_remove.append((str(net.ip), net.network.prefixlen))
        for addr_str in (family_inet6.get("address") or {}):
            net = ipaddress.ip_interface(addr_str)
            to_remove.append((str(net.ip), net.network.prefixlen))

        if not to_remove:
            return

        to_remove_set = set(to_remove)
        with self._iproute_factory() as ip:
            idx = self._lookup(ip, name)
            if idx is None:
                logger.warning("Interface %s not found; skipping address clear", name)
                return
            existing: list[tuple[str, int]] = [
                (msg.get_attr("IFA_ADDRESS"), msg["prefixlen"])
                for msg in ip.addr("dump", index=idx)
            ]
            for addr, plen in existing:
                if addr and (addr, plen) in to_remove_set:
                    ip.addr("del", index=idx, address=addr, prefixlen=plen)
                    logger.debug("Cleared address %s/%d from %s", addr, plen, name)

        if name in _nos_managed_addresses:
            _nos_managed_addresses[name] -= to_remove_set
            _save_managed_addresses()

    def sync_interface_addresses(self, name: str, config: Dict[str, Any]) -> None:
        """Sync IP addresses on an existing interface without touching state or attrs."""
        with self._iproute_factory() as ip:
            idx = self._lookup(ip, name)
            if idx is None:
                logger.warning("Interface %s not found; skipping address sync", name)
                return
            self._sync_addresses(ip, idx, name, config)

    def apply_subinterface(self, parent: str, unit_num: int, config: Dict[str, Any]) -> None:
        """Create or update a subinterface <parent>.<unit_num>.

        For loopback dummies (lo0, lo1, …) unit 0 maps to the parent interface
        itself; unit N > 0 creates a separate dummy <parent>.<unit_num>.
        For all other interfaces VLAN subinterfaces are used.
        """
        if _LOOPBACK_DUMMY_RE.match(parent):
            self._apply_loopback_unit(parent, unit_num, config)
            return

        vlan_id = config.get("vlan_id")
        sub_name = f"{parent}.{unit_num}"
        with self._iproute_factory() as ip:
            parent_idx = self._lookup(ip, parent)
            if parent_idx is None:
                logger.warning(
                    "Parent interface %s not found; skipping subinterface %s", parent, sub_name
                )
                return
            idx = self._lookup(ip, sub_name)
            if idx is None:
                if vlan_id is None:
                    logger.warning(
                        "Unit %d on %s has no vlan_id; cannot create subinterface",
                        unit_num, parent,
                    )
                    return
                ip.link("add", ifname=sub_name, kind="vlan", link=parent_idx, vlan_id=vlan_id)
                idx = self._lookup(ip, sub_name)
                if idx is None:
                    raise RuntimeError(f"Failed to create subinterface {sub_name}")
            self._sync_addresses(ip, idx, sub_name, config)
            self._apply_state(ip, idx, config)

    def apply_svi(self, name: str, config: Dict[str, Any]) -> None:
        """Create or update an SVI (VLAN interface on nos-br), e.g. irb.101."""
        _BRIDGE = "nos-br"

        vlan_id = config.get("vlan_id")
        if vlan_id is None:
            parts = name.split(".", 1)
            if len(parts) == 2 and parts[1].isdigit():
                vlan_id = int(parts[1])
        if vlan_id is None:
            logger.warning("SVI %s has no vlan_id; cannot create interface", name)
            return

        with self._iproute_factory() as ip:
            bridge_idx = self._lookup(ip, _BRIDGE)
            if bridge_idx is None:
                logger.warning("Bridge %s not found; skipping SVI %s", _BRIDGE, name)
                return

            idx = self._lookup(ip, name)
            if idx is None:
                ip.link("add", ifname=name, kind="vlan", link=bridge_idx, vlan_id=int(vlan_id))
                idx = self._lookup(ip, name)
                if idx is None:
                    raise RuntimeError(f"Failed to create SVI {name}")

            self._sync_addresses(ip, idx, name, config)
            self._apply_state(ip, idx, config)

    def _apply_loopback_unit(self, parent: str, unit_num: int, config: Dict[str, Any]) -> None:
        """Apply a loopback unit config.

        Unit 0 → sync addresses directly on the parent dummy (no kernel subinterface).
        Unit N > 0 → create <parent>.<unit_num> as a separate dummy interface.
        """
        target_name = parent if unit_num == 0 else f"{parent}.{unit_num}"
        with self._iproute_factory() as ip:
            idx = self._lookup(ip, target_name)
            if idx is None:
                if unit_num == 0:
                    logger.warning(
                        "Loopback interface %s not found; run apply_interface first", parent
                    )
                    return
                ip.link("add", ifname=target_name, kind="dummy")
                idx = self._lookup(ip, target_name)
                if idx is None:
                    raise RuntimeError(f"Failed to create loopback subinterface {target_name}")
                logger.debug("Created loopback dummy subinterface %s", target_name)
            self._sync_addresses(ip, idx, target_name, config)
            self._apply_state(ip, idx, config)

    def delete_interface(self, name: str) -> None:
        """Delete a virtual interface (no-op for physical interfaces)."""
        if self._is_physical(name):
            logger.debug("Refusing to delete physical interface %s", name)
            return
        with self._iproute_factory() as ip:
            idx = self._lookup(ip, name)
            if idx is None:
                return
            ip.link("set", index=idx, state="down")
            ip.link("del", index=idx)
            logger.debug("Deleted interface %s (idx=%d)", name, idx)

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _lookup(ip: IPRoute, name: str) -> Optional[int]:
        links = ip.link_lookup(ifname=name)
        return links[0] if links else None

    @staticmethod
    def _is_physical(name: str) -> bool:
        if "." in name:
            return False
        # lo0, lo1, … are NOS-managed dummies even though "lo" is in _PHYSICAL_PREFIXES.
        if _LOOPBACK_DUMMY_RE.match(name):
            return False
        return any(name.startswith(p) for p in _PHYSICAL_PREFIXES)

    @staticmethod
    def _create_dummy(ip: IPRoute, name: str) -> None:
        ip.link("add", ifname=name, kind="dummy")
        logger.debug("Created dummy interface %s", name)

    @staticmethod
    def _apply_attrs(ip: IPRoute, idx: int, config: Dict[str, Any]) -> None:
        attrs: Dict[str, Any] = {}

        description = config.get("description")
        if description is not None:
            attrs["ifalias"] = str(description)

        mtu = config.get("mtu")
        if mtu is not None:
            attrs["mtu"] = int(mtu)

        if attrs:
            ip.link("set", index=idx, **attrs)
            logger.debug("Set link attrs on idx=%d: %s", idx, attrs)

    @staticmethod
    def _sync_addresses(ip: IPRoute, idx: int, iface_name: str, config: Dict[str, Any]) -> None:
        family_inet = config.get("family_inet") or {}
        family_inet6 = config.get("family_inet6") or {}

        desired: list[tuple[str, int]] = []
        for addr_str in (family_inet.get("address") or {}):
            net = ipaddress.ip_interface(addr_str)
            desired.append((str(net.ip), net.network.prefixlen))
        for addr_str in (family_inet6.get("address") or {}):
            net = ipaddress.ip_interface(addr_str)
            desired.append((str(net.ip), net.network.prefixlen))

        nos_managed = _nos_managed_addresses.get(iface_name, set())

        # If NOS has never managed this interface and has nothing to configure, leave OS
        # addresses (netplan, DHCP, etc.) completely untouched.
        if not desired and not nos_managed:
            return

        existing: list[tuple[str, int]] = [
            (msg.get_attr("IFA_ADDRESS"), msg["prefixlen"])
            for msg in ip.addr("dump", index=idx)
        ]
        desired_set = set(desired)
        existing_set = {(a, p) for a, p in existing if a}

        # Remove only addresses NOS previously applied that are no longer desired.
        for addr, plen in existing:
            if addr and (addr, plen) in nos_managed and (addr, plen) not in desired_set:
                ip.addr("del", index=idx, address=addr, prefixlen=plen)
                logger.debug("Removed address %s/%d from idx=%d", addr, plen, idx)

        # Add newly configured addresses.
        for addr, plen in desired:
            if (addr, plen) not in existing_set:
                ip.addr("add", index=idx, address=addr, prefixlen=plen)
                logger.debug("Added address %s/%d to idx=%d", addr, plen, idx)

        # Record exactly what NOS has applied; used to guard removals on future syncs.
        _nos_managed_addresses[iface_name] = desired_set
        _save_managed_addresses()

    @staticmethod
    def _apply_state(ip: IPRoute, idx: int, config: Dict[str, Any]) -> None:
        disabled = config.get("disable", False)
        state = "down" if disabled else "up"
        ip.link("set", index=idx, state=state)
        logger.debug("Set interface idx=%d state=%s", idx, state)
