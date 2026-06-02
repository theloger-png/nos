from __future__ import annotations

from typing import Any, Dict, Optional

from nos.drivers.base import BaseDriver
from nos.drivers.kernel.bridge import BridgeDriver
from nos.drivers.kernel.interfaces import InterfaceDriver
from nos.drivers.kernel.routes import RouteDriver
from nos.drivers.kernel.vrf import VRFDriver


class KernelDriver(BaseDriver):
    """Composite kernel driver that delegates to InterfaceDriver, BridgeDriver, and RouteDriver."""

    def __init__(self) -> None:
        self._iface = InterfaceDriver()
        self._bridge = BridgeDriver()
        self._route = RouteDriver()

    def apply_interface(self, name: str, config: Dict[str, Any]) -> None:
        self._iface.apply_interface(name, config)

    def delete_interface(self, name: str) -> None:
        self._iface.delete_interface(name)

    def sync_interface_addresses(self, name: str, config: Dict[str, Any]) -> None:
        self._iface.sync_interface_addresses(name, config)

    def apply_subinterface(self, parent: str, unit_num: int, config: Dict[str, Any]) -> None:
        self._iface.apply_subinterface(parent, unit_num, config)

    def apply_route(self, prefix: str, config: Dict[str, Any], vrf: Optional[str] = None) -> None:
        self._route.apply_route(prefix, config)

    def delete_route(self, prefix: str, vrf: Optional[str] = None) -> None:
        self._route.delete_route(prefix)

    def apply_bridge(self, name: str, config: Dict[str, Any]) -> None:
        self._bridge.apply_bridge(name, config)

    def apply_vlan(self, bridge: str, port: str, config: Dict[str, Any]) -> None:
        self._bridge.apply_vlan(bridge, port, config)

    def apply_svi(self, name: str, config: Dict[str, Any]) -> None:
        pass  # not yet implemented


__all__ = ["BridgeDriver", "InterfaceDriver", "KernelDriver", "RouteDriver", "VRFDriver"]
