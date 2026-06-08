"""nftables NAT driver — manages the nos_nat table via the nft CLI."""
from __future__ import annotations

import ipaddress
import subprocess
from typing import Callable, Optional

from nos.utils.logger import get_logger

log = get_logger(__name__)

_NFT_TABLE = "nos_nat"
_NFT_FAMILY = "inet"


def _pool_range(prefix: str) -> tuple[str, str]:
    """Return (start_ip, end_ip) of the usable host range for *prefix*.

    Network and broadcast addresses are excluded.  For /32 (single host)
    the address itself is returned as both start and end.
    """
    net = ipaddress.ip_network(prefix, strict=False)
    hosts = list(net.hosts())
    if not hosts:
        addr = str(net.network_address)
        return (addr, addr)
    return (str(hosts[0]), str(hosts[-1]))


class NatDriver:
    """Manage nftables NAT rules for the nos_nat table.

    All rules are regenerated from the full NatConfig on each apply() call
    (stateless, full-replacement approach).
    """

    def apply(
        self,
        nat_config: "NatConfig",  # type: ignore[name-defined]  # noqa: F821
        alias_to_kernel_fn: Optional[Callable[[str], str]] = None,
    ) -> None:
        """Regenerate all nos_nat nftables rules from *nat_config*.

        *alias_to_kernel_fn* translates NOS interface aliases (et0) to kernel
        names (ens33).  When None, names are passed through unchanged.
        """
        def resolve(name: str) -> str:
            return alias_to_kernel_fn(name) if alias_to_kernel_fn else name

        prerouting: list[str] = []
        postrouting: list[str] = []

        # SNAT static (1:1)
        for _name, rule in nat_config.static.rule.items():
            if rule.source and rule.translated:
                postrouting.append(
                    f'        iifname != "lo" ip saddr {rule.source} '
                    f"snat to {rule.translated}"
                )

        # SNAT pool
        for _name, rule in nat_config.source.rule.items():
            if not (rule.match_source and rule.then_pool and rule.interface):
                continue
            pool = nat_config.pool.get(rule.then_pool)
            if pool is None or not pool.address:
                continue
            kernel_iface = resolve(rule.interface)
            start, end = _pool_range(pool.address)
            range_str = start if start == end else f"{start}-{end}"
            postrouting.append(
                f'        oifname "{kernel_iface}" ip saddr {rule.match_source} '
                f"snat to {range_str}"
            )

        # DNAT
        for _name, rule in nat_config.destination.rule.items():
            if not (rule.match_destination and rule.then_destination):
                continue
            if rule.match_destination_port is not None:
                for proto in ("tcp", "udp"):
                    dnat_target = rule.then_destination
                    if rule.then_destination_port is not None:
                        dnat_target = f"{rule.then_destination}:{rule.then_destination_port}"
                    prerouting.append(
                        f"        {proto} dport {rule.match_destination_port} "
                        f"ip daddr {rule.match_destination} dnat to {dnat_target}"
                    )
            else:
                prerouting.append(
                    f"        ip daddr {rule.match_destination} "
                    f"dnat to {rule.then_destination}"
                )

        has_rules = bool(prerouting or postrouting)

        if not has_rules:
            self.flush()
            return

        ruleset_lines: list[str] = [
            f"table {_NFT_FAMILY} {_NFT_TABLE} {{",
            "    chain prerouting {",
            f"        type nat hook prerouting priority dstnat; policy accept;",
        ]
        ruleset_lines.extend(prerouting)
        ruleset_lines += [
            "    }",
            "    chain postrouting {",
            f"        type nat hook postrouting priority srcnat; policy accept;",
        ]
        ruleset_lines.extend(postrouting)
        ruleset_lines += ["    }", "}"]

        ruleset = "\n".join(ruleset_lines) + "\n"

        # Flush existing table first so the replace is atomic.
        self.flush()

        log.debug("Applying nftables NAT ruleset:\n%s", ruleset)
        try:
            result = subprocess.run(
                ["sudo", "nft", "-f", "-"],
                input=ruleset,
                text=True,
                capture_output=True,
            )
            if result.returncode != 0:
                log.error(
                    "nft -f - failed (rc=%d): %s", result.returncode, result.stderr.strip()
                )
        except FileNotFoundError:
            log.error("nft not found in PATH; NAT rules not applied")

    def flush(self) -> None:
        """Remove the nos_nat table entirely (no-op if table does not exist)."""
        try:
            subprocess.run(
                ["sudo", "nft", "delete", "table", _NFT_FAMILY, _NFT_TABLE],
                capture_output=True,
            )
        except FileNotFoundError:
            log.debug("nft not found; skipping flush")
