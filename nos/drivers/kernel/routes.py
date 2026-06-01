from __future__ import annotations

import ipaddress
import logging
from typing import Any, Callable, Dict, Optional

from pyroute2 import IPRoute

logger = logging.getLogger(__name__)

# Linux routing table IDs
_TABLE_MAIN = 254
_TABLE_LOCAL = 255


class RouteDriver:
    """Manages static routes in the Linux kernel FIB via pyroute2.

    ``iproute_factory`` can be injected in tests.
    """

    def __init__(self, iproute_factory: Optional[Callable] = None) -> None:
        self._iproute_factory: Callable = iproute_factory or IPRoute

    # ------------------------------------------------------------------
    # Public interface
    # ------------------------------------------------------------------

    def apply_route(
        self,
        prefix: str,
        config: Dict[str, Any],
        table: int = _TABLE_MAIN,
    ) -> None:
        """Add or replace a static route.

        ``config`` keys (matching the NOS StaticRoute schema dict):
          - ``next_hop`` (str): gateway IP address.
          - ``discard`` (bool): install a blackhole route.
          - ``reject`` (bool): install a prohibit (ICMP unreachable) route.
        """
        net = ipaddress.ip_network(prefix, strict=False)
        dst = str(net)
        family = self._af(net)

        with self._iproute_factory() as ip:
            if config.get("discard"):
                ip.route("replace", dst=dst, family=family, type="blackhole", table=table)
                logger.debug("Installed blackhole route %s (table=%d)", dst, table)
            elif config.get("reject"):
                ip.route("replace", dst=dst, family=family, type="prohibit", table=table)
                logger.debug("Installed prohibit route %s (table=%d)", dst, table)
            else:
                gateway = config.get("next_hop")
                if not gateway:
                    raise ValueError(f"Route {prefix}: next_hop required when not discard/reject")
                ip.route("replace", dst=dst, family=family, gateway=gateway, table=table)
                logger.debug("Installed route %s via %s (table=%d)", dst, gateway, table)

    def delete_route(
        self,
        prefix: str,
        table: int = _TABLE_MAIN,
    ) -> None:
        """Remove a static route.  No-op if the route does not exist."""
        net = ipaddress.ip_network(prefix, strict=False)
        dst = str(net)
        family = self._af(net)

        with self._iproute_factory() as ip:
            try:
                ip.route("del", dst=dst, family=family, table=table)
                logger.debug("Deleted route %s (table=%d)", dst, table)
            except Exception as exc:
                # Ignore ENOENT — route was already absent.
                if "ENOENT" in str(exc) or "No such" in str(exc):
                    logger.debug("Route %s not found, nothing to delete", dst)
                else:
                    raise

    def apply_vrf_route(
        self,
        prefix: str,
        config: Dict[str, Any],
        vrf_table: int,
    ) -> None:
        """Add or replace a route inside a VRF routing table."""
        self.apply_route(prefix, config, table=vrf_table)

    def delete_vrf_route(self, prefix: str, vrf_table: int) -> None:
        """Remove a route from a VRF routing table."""
        self.delete_route(prefix, table=vrf_table)

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _af(net: ipaddress._BaseNetwork) -> int:
        import socket
        return socket.AF_INET6 if isinstance(net, ipaddress.IPv6Network) else socket.AF_INET
