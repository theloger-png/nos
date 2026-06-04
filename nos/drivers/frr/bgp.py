from __future__ import annotations

from typing import Any, Dict, List, Optional


class BGPGenerator:
    """Generates FRR BGP configuration stanzas from NOS config dicts.

    The caller is responsible for wrapping the output in a complete
    ``frr.conf`` using :class:`~nos.drivers.frr.renderer.FRRRenderer`.
    """

    def render(
        self,
        bgp_cfg: Dict[str, Any],
        asn: Optional[int] = None,
        router_id: Optional[str] = None,
    ) -> List[str]:
        """Return all FRR BGP stanzas.

        ``bgp_cfg`` is a serialised :class:`BgpConfig` dict.
        ``asn`` comes from ``routing-options.autonomous-system``.
        ``router_id`` comes from ``routing-options.router-id``.
        """
        if not asn:
            return []

        lines = [f"router bgp {asn}"]

        if router_id:
            lines.append(f" bgp router-id {router_id}")

        # Collect BGP-instance-level redistribute (global, not per peer-group).
        fi = bgp_cfg.get("family_inet") or {}
        inet_redist = [p for p, v in (fi.get("redistribute") or {}).items() if v]
        fi6 = bgp_cfg.get("family_inet6") or {}
        inet6_redist = [p for p, v in (fi6.get("redistribute") or {}).items() if v]

        # Emit redistribute lines exactly once — in the first group that renders
        # each address-family block.
        inet_redist_emitted = False
        inet6_redist_emitted = False

        for group_name, group in (bgp_cfg.get("group") or {}).items():
            will_emit_inet = bool(group.get("family_inet", {})) or not group.get("family_inet6")
            will_emit_inet6 = bool(group.get("family_inet6"))

            gr_inet_redist = inet_redist if (will_emit_inet and not inet_redist_emitted) else []
            gr_inet6_redist = inet6_redist if (will_emit_inet6 and not inet6_redist_emitted) else []

            if will_emit_inet:
                inet_redist_emitted = True
            if will_emit_inet6:
                inet6_redist_emitted = True

            lines.extend(self._render_group(group_name, group, asn, gr_inet_redist, gr_inet6_redist))

        lines.append("!")
        return lines

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _render_group(
        self,
        name: str,
        group: Dict[str, Any],
        local_asn: int,
        inet_redist: Optional[List[str]] = None,
        inet6_redist: Optional[List[str]] = None,
    ) -> List[str]:
        lines: List[str] = []

        # Peer-group declaration.
        lines.append(f" neighbor {name} peer-group")

        remote_as = group.get("peer_as") or group.get("local_as") or local_asn
        group_type = group.get("group_type") or ""
        if group_type == "internal":
            remote_as = group.get("local_as") or local_asn
        lines.append(f" neighbor {name} remote-as {remote_as}")

        local_interface = group.get("local_interface")
        local_addr = group.get("local_address")
        if local_interface:
            lines.append(f" neighbor {name} update-source {local_interface}")
        elif local_addr:
            lines.append(f" neighbor {name} update-source {local_addr}")

        # Individual neighbors.
        for peer_ip, peer_cfg in (group.get("neighbor") or {}).items():
            lines.append(f" neighbor {peer_ip} peer-group {name}")
            desc = (peer_cfg or {}).get("description")
            if desc:
                lines.append(f" neighbor {peer_ip} description {desc}")
            auth = (peer_cfg or {}).get("authentication_key")
            if auth:
                lines.append(f" neighbor {peer_ip} password {auth}")
            ht = (peer_cfg or {}).get("hold_time")
            if ht is not None:
                lines.append(f" neighbor {peer_ip} timers-connect 0")
                lines.append(f" neighbor {peer_ip} timers 0 {ht}")

        # Address families.
        if group.get("family_inet", {}) or not group.get("family_inet6"):
            lines.append("  !")
            lines.append(" address-family ipv4 unicast")
            for proto in (inet_redist or []):
                lines.append(f"  redistribute {proto}")
            lines.append(f"  neighbor {name} activate")
            export = group.get("export")
            if export:
                lines.append(f"  neighbor {name} route-map {export} out")
            import_pol = group.get("import_policy")
            if import_pol:
                lines.append(f"  neighbor {name} route-map {import_pol} in")
            lines.append(" exit-address-family")

        if group.get("family_inet6"):
            lines.append("  !")
            lines.append(" address-family ipv6 unicast")
            for proto in (inet6_redist or []):
                lines.append(f"  redistribute {proto}")
            lines.append(f"  neighbor {name} activate")
            lines.append(" exit-address-family")

        return lines
