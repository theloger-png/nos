from __future__ import annotations

import logging
from typing import Any, Callable, Dict, List, Optional

from pyroute2 import IPRoute

logger = logging.getLogger(__name__)

# VLAN filter flags (from kernel uapi/linux/if_bridge.h)
_BRIDGE_VLAN_INFO_PVID = 0x2      # native / port VLAN
_BRIDGE_VLAN_INFO_UNTAGGED = 0x4  # egress untagged


class BridgeDriver:
    """Manages Linux bridge and VLAN filtering via pyroute2.

    A single bridge (``nos-br`` by default) is used for all switched ports.
    VLAN filtering is enabled on the bridge so each port can carry different
    VLAN memberships.

    ``iproute_factory`` can be injected in tests.
    """

    def __init__(
        self,
        bridge_name: str = "nos-br",
        iproute_factory: Optional[Callable] = None,
    ) -> None:
        self.bridge_name = bridge_name
        self._iproute_factory: Callable = iproute_factory or IPRoute

    # ------------------------------------------------------------------
    # Bridge lifecycle
    # ------------------------------------------------------------------

    def apply_bridge(self, name: str, config: Dict[str, Any]) -> None:
        """Create the bridge if it does not exist and bring it up.

        ``config`` keys:
          - ``ports`` (list[str]): interface names to add as bridge members.
          - ``vlan_filtering`` (bool): enable 802.1Q VLAN filtering (default True).
        """
        with self._iproute_factory() as ip:
            br_idx = self._lookup(ip, name)
            if br_idx is None:
                vlan_filter = int(config.get("vlan_filtering", True))
                ip.link(
                    "add",
                    ifname=name,
                    kind="bridge",
                    br_vlan_filtering=vlan_filter,
                )
                br_idx = self._lookup(ip, name)
                if br_idx is None:
                    raise RuntimeError(f"Failed to create bridge {name}")
                logger.debug("Created bridge %s (idx=%d)", name, br_idx)

            ip.link("set", index=br_idx, state="up")

            for port in config.get("ports") or []:
                self._add_port(ip, br_idx, port)

    def delete_bridge(self, name: str) -> None:
        """Bring down and delete a bridge interface."""
        with self._iproute_factory() as ip:
            idx = self._lookup(ip, name)
            if idx is None:
                return
            ip.link("set", index=idx, state="down")
            ip.link("del", index=idx)
            logger.debug("Deleted bridge %s", name)

    # ------------------------------------------------------------------
    # Port / VLAN membership
    # ------------------------------------------------------------------

    def apply_vlan(self, bridge: str, port: str, config: Dict[str, Any]) -> None:
        """Configure VLAN membership for a bridge port.

        ``config`` keys:
          - ``interface_mode`` (str): ``"access"`` or ``"trunk"``.
          - ``vlans`` (list[int|str]): VLAN IDs (``"all"`` means 1-4094).
          - ``native_vlan`` (int): native/untagged VLAN for trunk ports.
        """
        with self._iproute_factory() as ip:
            br_idx = self._lookup(ip, bridge)
            if br_idx is None:
                raise ValueError(f"Bridge {bridge!r} does not exist")

            port_idx = self._lookup(ip, port)
            if port_idx is None:
                raise ValueError(f"Interface {port!r} does not exist")

            # Ensure port is a member of the bridge.
            self._add_port(ip, br_idx, port)

            mode = config.get("interface_mode", "access")
            native = config.get("native_vlan", 1)
            vlans = self._resolve_vlans(config.get("vlans") or [], mode)

            # Clear existing VLAN entries on this port.
            self._flush_vlan_filters(ip, port_idx)

            if mode == "access":
                # Access: single VLAN, egress untagged + set as PVID.
                for vid in vlans:
                    flags = _BRIDGE_VLAN_INFO_PVID | _BRIDGE_VLAN_INFO_UNTAGGED
                    ip.vlan_filter("add", index=port_idx, vlan_info={"vid": vid, "flags": flags})
                    logger.debug("Set access VLAN %d on %s", vid, port)
            else:
                # Trunk: all listed VLANs tagged, native VLAN untagged.
                for vid in vlans:
                    flags = 0
                    if vid == native:
                        flags = _BRIDGE_VLAN_INFO_PVID | _BRIDGE_VLAN_INFO_UNTAGGED
                    ip.vlan_filter("add", index=port_idx, vlan_info={"vid": vid, "flags": flags})
                logger.debug("Set trunk VLANs %s (native=%d) on %s", vlans, native, port)

    def detach_port(self, bridge: str, port: str) -> None:
        """Remove a port from the bridge by setting its master to 0."""
        with self._iproute_factory() as ip:
            port_idx = self._lookup(ip, port)
            if port_idx is None:
                logger.warning("Port %s not found; skipping detach", port)
                return
            ip.link("set", index=port_idx, master=0)
            logger.debug("Detached %s (idx=%d) from bridge", port, port_idx)

    def get_bridge_ports(self, name: str) -> List[str]:
        """Return names of interfaces currently attached to the bridge."""
        with self._iproute_factory() as ip:
            br_idx = self._lookup(ip, name)
            if br_idx is None:
                return []
            ports = []
            for link in ip.get_links():
                if (link.get_attr("IFLA_MASTER") or 0) == br_idx:
                    ifname = link.get_attr("IFLA_IFNAME")
                    if ifname:
                        ports.append(ifname)
            return ports

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _add_port(self, ip: IPRoute, br_idx: int, port_name: str) -> None:
        port_idx = self._lookup(ip, port_name)
        if port_idx is None:
            logger.warning("Port %s not found; skipping bridge attachment", port_name)
            return
        current = ip.link("get", index=port_idx)
        existing_master = 0
        if current:
            existing_master = current[0].get_attr("IFLA_MASTER") or 0
        if existing_master != br_idx:
            ip.link("set", index=port_idx, master=br_idx, state="up")
            logger.debug("Added %s (idx=%d) to bridge idx=%d", port_name, port_idx, br_idx)

    @staticmethod
    def _lookup(ip: IPRoute, name: str) -> Optional[int]:
        links = ip.link_lookup(ifname=name)
        return links[0] if links else None

    @staticmethod
    def _resolve_vlans(vlans: List[Any], mode: str) -> List[int]:
        """Expand the VLAN list, treating ``"all"`` as 1-4094."""
        if not vlans or vlans == ["all"] or "all" in vlans:
            return list(range(1, 4095))
        result: List[int] = []
        for v in vlans:
            if isinstance(v, str) and v.isdigit():
                result.append(int(v))
            elif isinstance(v, int):
                result.append(v)
        return result

    @staticmethod
    def _flush_vlan_filters(ip: IPRoute, port_idx: int) -> None:
        """Remove all existing VLAN filter entries from a bridge port."""
        for vid in range(1, 4095):
            try:
                ip.vlan_filter("del", index=port_idx, vlan_info={"vid": vid, "flags": 0})
            except Exception:
                pass
