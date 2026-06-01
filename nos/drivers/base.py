from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any, Dict, Optional


class BaseDriver(ABC):
    """Abstract base driver interface for all NOS backend operations."""

    @abstractmethod
    def apply_interface(self, name: str, config: Dict[str, Any]) -> None:
        """Create or update a network interface with the given configuration."""

    @abstractmethod
    def delete_interface(self, name: str) -> None:
        """Delete a network interface."""

    @abstractmethod
    def apply_route(
        self,
        prefix: str,
        config: Dict[str, Any],
        vrf: Optional[str] = None,
    ) -> None:
        """Add or replace a static route."""

    @abstractmethod
    def delete_route(self, prefix: str, vrf: Optional[str] = None) -> None:
        """Delete a route for the given prefix."""

    @abstractmethod
    def apply_bridge(self, name: str, config: Dict[str, Any]) -> None:
        """Create or update a bridge interface with VLAN filtering."""

    @abstractmethod
    def apply_vlan(self, bridge: str, port: str, config: Dict[str, Any]) -> None:
        """Configure VLAN membership for a port on a bridge."""

    @abstractmethod
    def apply_svi(self, name: str, config: Dict[str, Any]) -> None:
        """Create or update an SVI (IRB) interface with IP addressing."""
