from __future__ import annotations

from typing import Any, Dict

from nos.drivers.frr.bgp import BGPGenerator
from nos.drivers.frr.isis import ISISGenerator


class FRRRenderer:
    """Renders a NOS configuration dict into a complete FRR ``frr.conf`` string.

    The rendered file is suitable for writing to ``/etc/frr/frr.conf`` and
    loading via :class:`~nos.drivers.frr.client.FRRClient`.
    """

    def __init__(self) -> None:
        self._isis = ISISGenerator()
        self._bgp = BGPGenerator()

    def render(self, config: Dict[str, Any]) -> str:
        """Return a full frr.conf string for *config*.

        ``config`` is a plain dict matching the NOSConfig schema (i.e. the
        result of ``NOSConfig(**data).model_dump()``).
        """
        lines: list[str] = []

        hostname = (config.get("system") or {}).get("host_name", "nos")
        routing_opts = config.get("routing_options") or {}
        router_id: str | None = routing_opts.get("router_id")
        asn: int | None = routing_opts.get("autonomous_system")
        protocols = config.get("protocols") or {}
        isis_cfg = protocols.get("isis")
        bgp_cfg = protocols.get("bgp")

        # FRR header.
        lines += [
            "frr version 8.0",
            "frr defaults traditional",
            f"hostname {hostname}",
            "log syslog informational",
            "no ipv6 forwarding",
            "!",
        ]

        # Interface stanzas needed by IS-IS.
        if isis_cfg:
            for iface_name, iface_cfg in (isis_cfg.get("interface") or {}).items():
                lines += self._isis.render_interface(iface_name, iface_cfg or {})

        # IS-IS router block.
        if isis_cfg:
            lines += self._isis.render_router(isis_cfg, router_id=router_id)

        # BGP router block.
        if bgp_cfg:
            lines += self._bgp.render(bgp_cfg, asn=asn, router_id=router_id)

        lines.append("")  # trailing newline
        return "\n".join(lines)
