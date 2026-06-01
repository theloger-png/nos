from __future__ import annotations

import ipaddress
import logging
from typing import Any, Callable, Dict, Optional

from pyroute2 import IPRoute

logger = logging.getLogger(__name__)

# Physical / pre-existing interface name prefixes — skip creation for these.
_PHYSICAL_PREFIXES = ("eth", "ens", "enp", "eno", "lo", "bond", "team")


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
            self._sync_addresses(ip, idx, config)
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
    def _sync_addresses(ip: IPRoute, idx: int, config: Dict[str, Any]) -> None:
        family_inet = config.get("family_inet") or {}
        family_inet6 = config.get("family_inet6") or {}

        desired: list[tuple[str, int]] = []
        for addr_str in (family_inet.get("address") or {}):
            net = ipaddress.ip_interface(addr_str)
            desired.append((str(net.ip), net.network.prefixlen))
        for addr_str in (family_inet6.get("address") or {}):
            net = ipaddress.ip_interface(addr_str)
            desired.append((str(net.ip), net.network.prefixlen))

        if not desired:
            return

        existing: list[tuple[str, int]] = [
            (msg.get_attr("IFA_ADDRESS"), msg["prefixlen"])
            for msg in ip.addr("dump", index=idx)
        ]

        # Remove addresses no longer configured.
        for addr, plen in existing:
            if addr and (addr, plen) not in desired:
                ip.addr("del", index=idx, address=addr, prefixlen=plen)
                logger.debug("Removed address %s/%d from idx=%d", addr, plen, idx)

        # Add newly configured addresses.
        for addr, plen in desired:
            if (addr, plen) not in existing:
                ip.addr("add", index=idx, address=addr, prefixlen=plen)
                logger.debug("Added address %s/%d to idx=%d", addr, plen, idx)

    @staticmethod
    def _apply_state(ip: IPRoute, idx: int, config: Dict[str, Any]) -> None:
        disabled = config.get("disable", False)
        state = "down" if disabled else "up"
        ip.link("set", index=idx, state=state)
        logger.debug("Set interface idx=%d state=%s", idx, state)
