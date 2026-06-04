from __future__ import annotations

import ipaddress
from typing import Any, Dict, Optional

from nos.drivers.frr.bgp import BGPGenerator
from nos.drivers.frr.isis import ISISGenerator


def _has_ip_addresses(iface: Dict[str, Any]) -> bool:
    inet = (iface.get("family_inet") or {}).get("address") or {}
    inet6 = (iface.get("family_inet6") or {}).get("address") or {}
    return bool(inet or inet6)


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
        router_id: Optional[str] = routing_opts.get("router_id")
        asn: Optional[int] = routing_opts.get("autonomous_system")
        interfaces_cfg: Dict[str, Any] = config.get("interfaces") or {}
        protocols = config.get("protocols") or {}
        isis_cfg = protocols.get("isis")
        bgp_cfg = protocols.get("bgp")

        # Derive router_id from lo0's first IPv4 address when not explicit.
        if not router_id:
            lo0_cfg = interfaces_cfg.get("lo0") or {}
            lo0_addrs = (lo0_cfg.get("family_inet") or {}).get("address") or {}
            if lo0_addrs:
                first_addr = next(iter(lo0_addrs))
                router_id = str(ipaddress.ip_interface(first_addr).ip)

        # FRR header.
        lines += [
            "frr version 8.0",
            "frr defaults traditional",
            f"hostname {hostname}",
            "log syslog informational",
            "no ipv6 forwarding",
            "!",
        ]

        # Interface stanzas: one merged block per interface, covering both
        # IP address assignment and IS-IS interface configuration.
        isis_ifaces: Dict[str, Any] = (isis_cfg or {}).get("interface") or {}
        all_iface_names: set[str] = set(isis_ifaces.keys())
        for name, iface in interfaces_cfg.items():
            if _has_ip_addresses(iface):
                all_iface_names.add(name)

        for iface_name in sorted(all_iface_names):
            iface_data = interfaces_cfg.get(iface_name) or {}
            isis_iface_cfg: Optional[Dict[str, Any]] = (
                (isis_ifaces.get(iface_name) or {}) if iface_name in isis_ifaces else None
            )
            lines += self._render_interface_stanza(iface_name, iface_data, isis_iface_cfg)

        # IS-IS router block.
        if isis_cfg:
            lines += self._isis.render_router(isis_cfg, router_id=router_id)

        # BGP router block.
        if bgp_cfg:
            lines += self._bgp.render(bgp_cfg, asn=asn, router_id=router_id)

        lines.append("")  # trailing newline
        return "\n".join(lines)

    def _render_interface_stanza(
        self,
        iface_name: str,
        iface_data: Dict[str, Any],
        isis_iface_cfg: Optional[Dict[str, Any]],
    ) -> list[str]:
        lines = [f"interface {iface_name}"]

        for addr in (iface_data.get("family_inet") or {}).get("address") or {}:
            lines.append(f" ip address {addr}")
        for addr in (iface_data.get("family_inet6") or {}).get("address") or {}:
            lines.append(f" ipv6 address {addr}")

        if isis_iface_cfg is not None:
            lines += self._isis.render_interface_body(iface_name, isis_iface_cfg)

        lines.append("!")
        return lines
