from __future__ import annotations

import ipaddress
from typing import Any, Dict, Optional

from nos.config.serializer import _k2j
from nos.drivers.frr.bgp import BGPGenerator
from nos.drivers.frr.isis import ISISGenerator


def _has_ip_addresses(iface: Dict[str, Any]) -> bool:
    inet = (iface.get("family_inet") or {}).get("address") or {}
    inet6 = (iface.get("family_inet6") or {}).get("address") or {}
    iso_addr = (iface.get("family_iso") or {}).get("address")
    return bool(inet or inet6 or iso_addr)


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

        # Route-map stanzas (must precede the BGP block so references resolve).
        policy_options = config.get("policy_options") or {}
        if policy_options:
            lines += self._render_policy_options(policy_options)

        # BGP router block.
        if bgp_cfg:
            lines += self._bgp.render(bgp_cfg, asn=asn, router_id=router_id)

        lines.append("")  # trailing newline
        return "\n".join(lines)

    # FRR protocol name differs from NOS for "direct" routes.
    _PROTO_MAP: dict[str, str] = {"direct": "connected"}

    def _render_policy_options(self, policy_options: Dict[str, Any]) -> list[str]:
        """Render all policy-statements as FRR route-map stanzas."""
        lines: list[str] = []
        for ps_name in sorted(policy_options.get("policy_statement") or {}):
            ps_data = (policy_options["policy_statement"][ps_name]) or {}
            lines += self._render_policy_statement(ps_name, ps_data)
        return lines

    def _render_policy_statement(self, name: str, ps_data: Dict[str, Any]) -> list[str]:
        """Render one policy-statement as a series of route-map entries.

        Named terms are emitted first (seq 10, 20, …).  The unnamed final
        term (``then`` directly on the policy-statement) is always last at
        seq 65535.  ``next-policy`` produces no route-map entry; FRR will
        fall through to its own default behaviour.
        """
        lines: list[str] = []
        seq = 10
        # Convert policy name from underscores to hyphens to match BGP references
        route_map_name = _k2j(name)

        for term_data in (ps_data.get("term") or {}).values():
            term = term_data or {}
            # Support both CLI-generated keys (from/then) and schema keys
            # (from_config/then_config) so the renderer works regardless of
            # which path was used to populate the config store.
            then = term.get("then") or term.get("then_config") or {}
            from_cfg = term.get("from") or term.get("from_config") or {}

            action = "permit" if then.get("accept") else "deny"
            lines.append(f"route-map {route_map_name} {action} {seq}")

            if from_cfg.get("protocol"):
                frr_proto = self._PROTO_MAP.get(from_cfg["protocol"], from_cfg["protocol"])
                lines.append(f" match source-protocol {frr_proto}")

            if from_cfg.get("prefix_list"):
                lines.append(f" match ip address prefix-list {from_cfg['prefix_list']}")

            lines.append("!")
            seq += 10

        # Final (unnamed) term.
        final_then = ps_data.get("then") or {}
        if final_then.get("accept"):
            lines.append(f"route-map {route_map_name} permit 65535")
            lines.append("!")
        elif final_then.get("reject"):
            lines.append(f"route-map {route_map_name} deny 65535")
            lines.append("!")
        # next_policy: no entry; FRR continues past the route-map by default.

        return lines

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
        iso_addr = (iface_data.get("family_iso") or {}).get("address")
        if iso_addr:
            lines.append(f" iso enable")
            lines.append(f" iso address {iso_addr}")

        if isis_iface_cfg is not None:
            lines += self._isis.render_interface_body(iface_name, isis_iface_cfg)

        lines.append("!")
        return lines
