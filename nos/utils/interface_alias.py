"""Interface aliasing: map kernel interface names (ens33) to NOS aliases (et0).

When system.interface_rename is enabled, physical interfaces are presented as
et0, et1, etc. everywhere in nos-cli.  The mapping is persisted to disk so it
survives reboots without re-detection.
"""
from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any, Optional

_DEFAULT_MAP_PATH = "/opt/nos/interface_map.json"

_SKIP_PREFIXES = ("irb", "nos-", "bond", "vlan", "lo")

_NON_PHYSICAL_KINDS = frozenset({
    "bridge", "bond", "tun", "tap", "dummy", "veth", "vlan",
    "macvlan", "macvtap", "ipvlan", "vxlan", "gre", "gretap",
    "sit", "ipip", "ip6tnl", "ip6gre", "wireguard", "vrf",
})


def detect_physical_interfaces() -> list[str]:
    """Return physical Ethernet interface names sorted by ifindex.

    Filters out: subinterfaces (dotted names), loopback, and names starting
    with irb, nos-, bond, or vlan.  Soft interfaces (bridge, tun, etc.) are
    excluded by checking IFLA_INFO_KIND.
    """
    from pyroute2 import IPRoute

    result: list[tuple[int, str]] = []
    with IPRoute() as ipr:
        for link in ipr.get_links():
            name = link.get_attr("IFLA_IFNAME")
            if not name:
                continue
            if "." in name:
                continue
            if any(name.startswith(p) for p in _SKIP_PREFIXES):
                continue
            # ARPHRD_ETHER = 1
            if link.get("ifi_type") != 1:
                continue
            linkinfo = link.get_attr("IFLA_LINKINFO")
            if linkinfo is not None:
                kind = linkinfo.get_attr("IFLA_INFO_KIND") or ""
                if kind in _NON_PHYSICAL_KINDS:
                    continue
            result.append((link["index"], name))

    result.sort(key=lambda x: x[0])
    return [name for _, name in result]


def generate_alias_map(physical_ifaces: list[str]) -> dict[str, str]:
    """Return {physical_name: alias} for the given interface list.

    Example: ["ens33", "ens34"] → {"ens33": "et0", "ens34": "et1"}
    """
    return {name: f"et{i}" for i, name in enumerate(physical_ifaces)}


def save_alias_map(
    alias_map: dict[str, str],
    path: str = _DEFAULT_MAP_PATH,
) -> None:
    """Persist alias_map to disk as JSON."""
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    with open(p, "w") as fh:
        json.dump(alias_map, fh, indent=2)


def load_alias_map(path: str = _DEFAULT_MAP_PATH) -> Optional[dict[str, str]]:
    """Load alias_map from disk; return None if file does not exist."""
    p = Path(path)
    if not p.exists():
        return None
    with open(p) as fh:
        return json.load(fh)


def get_alias_map(path: str = _DEFAULT_MAP_PATH) -> dict[str, str]:
    """Return alias_map, loading from disk if available; detect on the fly otherwise."""
    cached = load_alias_map(path)
    if cached is not None:
        return cached
    physical = detect_physical_interfaces()
    return generate_alias_map(physical)


def to_alias(name: str, alias_map: dict[str, str]) -> str:
    """Translate a physical interface name to its alias.

    Handles subinterfaces: "ens34.101" → "et1.101".
    Returns name unchanged if no mapping exists.
    """
    if name in alias_map:
        return alias_map[name]
    if "." in name:
        base, suffix = name.split(".", 1)
        if base in alias_map:
            return f"{alias_map[base]}.{suffix}"
    return name


def to_physical(name: str, alias_map: dict[str, str]) -> str:
    """Translate an alias back to the physical interface name.

    Handles subinterfaces: "et1.101" → "ens34.101".
    Returns name unchanged if no mapping exists.
    """
    reverse: dict[str, str] = {v: k for k, v in alias_map.items()}
    return to_alias(name, reverse)


def _translate_keys(d: dict, alias_map: dict[str, str]) -> dict:
    """Return a new dict with all keys translated through to_alias."""
    return {to_alias(k, alias_map): v for k, v in d.items()}


def _translate_values_inplace(obj: Any, alias_map: dict[str, str]) -> None:
    """Recursively translate string values in *obj* that match alias_map keys.

    Only translates exact string matches (no substring replacement), so
    descriptions and IP addresses are left untouched.
    """
    if isinstance(obj, dict):
        for k in list(obj.keys()):
            v = obj[k]
            if isinstance(v, str):
                new_v = to_alias(v, alias_map)
                if new_v != v:
                    obj[k] = new_v
            elif isinstance(v, (dict, list)):
                _translate_values_inplace(v, alias_map)
    elif isinstance(obj, list):
        for i, item in enumerate(obj):
            if isinstance(item, str):
                new_v = to_alias(item, alias_map)
                if new_v != item:
                    obj[i] = new_v
            elif isinstance(item, (dict, list)):
                _translate_values_inplace(item, alias_map)


def migrate_config(cfg: dict, alias_map: dict[str, str]) -> dict:
    """Return a new config dict with all interface names translated to aliases.

    Performs three passes:
    1. Translate keys in the top-level ``interfaces`` section.
    2. Translate keys in ``protocols.isis.interface``.
    3. Translate all string values anywhere that exactly match a physical name
       (covers routing_instances interface lists and other references).

    Does not mutate the original; returns a deep copy.
    """
    result = copy.deepcopy(cfg)

    if isinstance(result.get("interfaces"), dict):
        result["interfaces"] = _translate_keys(result["interfaces"], alias_map)

    try:
        isis_ifaces = result["protocols"]["isis"]["interface"]
        if isinstance(isis_ifaces, dict):
            result["protocols"]["isis"]["interface"] = _translate_keys(
                isis_ifaces, alias_map
            )
    except (KeyError, TypeError):
        pass

    _translate_values_inplace(result, alias_map)
    return result


def migrate_config_reverse(cfg: dict, alias_map: dict[str, str]) -> dict:
    """Return a new config dict with alias names translated back to physical.

    Inverse of :func:`migrate_config`.
    """
    reverse: dict[str, str] = {v: k for k, v in alias_map.items()}
    return migrate_config(cfg, reverse)
