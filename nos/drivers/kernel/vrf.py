from __future__ import annotations

import logging
import zlib
from typing import Callable, Dict, List, Optional

from pyroute2 import IPRoute

logger = logging.getLogger(__name__)

# VRF routing table IDs are allocated in this range.
_TABLE_BASE = 1000
_TABLE_MAX = 9999


def vrf_table_id(name: str) -> int:
    """Return a stable routing table ID derived from the VRF name.

    IDs are in the range [1000, 9999] and are deterministic across restarts.
    """
    return _TABLE_BASE + (zlib.crc32(name.encode()) & 0x7FFF_FFFF) % (
        _TABLE_MAX - _TABLE_BASE + 1
    )


class VRFDriver:
    """Manages Linux VRF devices via pyroute2.

    Each VRF is backed by a Linux VRF master device which owns a private
    routing table.  Interfaces are enslaved to the VRF master device to
    participate in its routing table.

    ``iproute_factory`` can be injected in tests.
    """

    def __init__(self, iproute_factory: Optional[Callable] = None) -> None:
        self._iproute_factory: Callable = iproute_factory or IPRoute

    # ------------------------------------------------------------------
    # VRF lifecycle
    # ------------------------------------------------------------------

    def apply_vrf(self, name: str, interfaces: Optional[List[str]] = None) -> int:
        """Create a VRF device (if absent) and enslave the given interfaces.

        Returns the routing table ID associated with this VRF.
        """
        table = vrf_table_id(name)
        with self._iproute_factory() as ip:
            idx = self._lookup(ip, name)
            if idx is None:
                ip.link("add", ifname=name, kind="vrf", vrf_table=table)
                idx = self._lookup(ip, name)
                if idx is None:
                    raise RuntimeError(f"Failed to create VRF {name}")
                logger.debug("Created VRF %s (table=%d, idx=%d)", name, table, idx)
            ip.link("set", index=idx, state="up")

            for iface in interfaces or []:
                self._enslave(ip, idx, iface)

        return table

    def delete_vrf(self, name: str) -> None:
        """Bring down and remove a VRF device.

        Enslaved interfaces are automatically released by the kernel when the
        VRF master device is deleted.
        """
        with self._iproute_factory() as ip:
            idx = self._lookup(ip, name)
            if idx is None:
                return
            ip.link("set", index=idx, state="down")
            ip.link("del", index=idx)
            logger.debug("Deleted VRF %s", name)

    def assign_interface(self, vrf_name: str, iface_name: str) -> None:
        """Enslave an interface into an existing VRF."""
        with self._iproute_factory() as ip:
            vrf_idx = self._lookup(ip, vrf_name)
            if vrf_idx is None:
                raise ValueError(f"VRF {vrf_name!r} does not exist")
            self._enslave(ip, vrf_idx, iface_name)

    def release_interface(self, iface_name: str) -> None:
        """Remove an interface from its VRF (set master to 0 = no master)."""
        with self._iproute_factory() as ip:
            iface_idx = self._lookup(ip, iface_name)
            if iface_idx is None:
                logger.warning("Interface %s not found; skipping VRF release", iface_name)
                return
            ip.link("set", index=iface_idx, master=0)
            logger.debug("Released %s from VRF", iface_name)

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _enslave(self, ip: IPRoute, vrf_idx: int, iface_name: str) -> None:
        iface_idx = self._lookup(ip, iface_name)
        if iface_idx is None:
            logger.warning("Interface %s not found; skipping VRF enslave", iface_name)
            return
        # Check current master to avoid redundant netlink messages.
        links = ip.link("get", index=iface_idx)
        current_master = 0
        if links:
            current_master = links[0].get_attr("IFLA_MASTER") or 0
        if current_master != vrf_idx:
            ip.link("set", index=iface_idx, master=vrf_idx, state="up")
            logger.debug("Enslaved %s (idx=%d) to VRF idx=%d", iface_name, iface_idx, vrf_idx)

    @staticmethod
    def _lookup(ip: IPRoute, name: str) -> Optional[int]:
        links = ip.link_lookup(ifname=name)
        return links[0] if links else None
