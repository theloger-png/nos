from __future__ import annotations

import ipaddress
from typing import Any, Dict, List, Optional


def _router_id_to_net(router_id: str, area: str = "49.0001") -> str:
    """Derive an IS-IS NET from an IPv4 router-id.

    The 4 router-id bytes are zero-padded to a 6-byte system ID and formatted
    as three groups of four hex digits, e.g.::

        router_id="1.1.1.1"  →  NET "49.0001.0000.0101.0101.00"
    """
    packed = ipaddress.IPv4Address(router_id).packed  # 4 bytes
    sys_id = (b"\x00\x00" + packed).hex()             # 12 hex chars
    formatted = f"{sys_id[0:4]}.{sys_id[4:8]}.{sys_id[8:12]}"
    return f"{area}.{formatted}.00"


class ISISGenerator:
    """Generates FRR IS-IS configuration stanzas from NOS config dicts.

    The caller is responsible for wrapping the output in a complete
    ``frr.conf`` using :class:`~nos.drivers.frr.renderer.FRRRenderer`.
    """

    def render_interface(self, iface_name: str, iface_cfg: Dict[str, Any]) -> List[str]:
        """Return FRR interface-level IS-IS stanzas for *iface_name*.

        ``iface_cfg`` corresponds to a serialised :class:`IsisInterfaceConfig`.
        """
        lines = [f"interface {iface_name}"]
        lines.append(" ip router isis default")
        if iface_cfg.get("point_to_point"):
            lines.append(" isis point-to-point")
        hi = iface_cfg.get("hello_interval")
        if hi is not None:
            lines.append(f" isis hello-interval {hi}")
        ht = iface_cfg.get("hold_time")
        if ht is not None:
            lines.append(f" isis hold-time {ht}")
        lines.append("!")
        return lines

    def render_router(
        self,
        isis_cfg: Dict[str, Any],
        router_id: Optional[str] = None,
    ) -> List[str]:
        """Return the ``router isis default`` stanza.

        ``isis_cfg`` corresponds to a serialised :class:`IsisConfig`.
        ``router_id`` is taken from ``routing-options.router-id`` and used
        to derive the IS-IS NET when no explicit NET is configured.
        """
        lines = ["router isis default"]

        net = None
        if router_id:
            net = _router_id_to_net(router_id)
        if net:
            lines.append(f" net {net}")

        # Determine IS type from level config.
        l1 = isis_cfg.get("level_1") or {}
        l2 = isis_cfg.get("level_2") or {}
        level_1_disable = any(
            (ifc.get("level_1_disable") or False)
            for ifc in (isis_cfg.get("interface") or {}).values()
        )
        level_2_disable = any(
            (ifc.get("level_2_disable") or False)
            for ifc in (isis_cfg.get("interface") or {}).values()
        )
        if level_1_disable and not level_2_disable:
            lines.append(" is-type level-2-only")
        elif level_2_disable and not level_1_disable:
            lines.append(" is-type level-1-only")

        if l1.get("wide_metrics_only") or l2.get("wide_metrics_only"):
            lines.append(" metric-style wide")

        lines.append("!")
        return lines
