"""Show commands for security NAT (show security nat ...)."""
from __future__ import annotations

import subprocess
from typing import Any


def _nat_cfg(running: dict) -> dict:
    """Extract nat config dict from running config, return {} if absent."""
    return (running.get("security") or {}).get("nat") or {}


def show_nat_static(running: dict) -> str:
    """Render 'show security nat static'."""
    nat = _nat_cfg(running)
    rules: dict[str, Any] = (nat.get("static") or {}).get("rule") or {}
    if not rules:
        return "No static NAT rules configured."

    hdr = f"{'Rule':<16}{'Source':<23}Translated"
    sep = "-" * 55
    lines = ["Static NAT rules:", hdr, sep]
    for name, rule in sorted(rules.items()):
        if not isinstance(rule, dict):
            continue
        source = rule.get("source", "")
        translated = rule.get("translated", "")
        lines.append(f"{name:<16}{source:<23}{translated}")
    return "\n".join(lines)


def show_nat_source(running: dict) -> str:
    """Render 'show security nat source'."""
    nat = _nat_cfg(running)
    rules: dict[str, Any] = (nat.get("source") or {}).get("rule") or {}
    if not rules:
        return "No source NAT rules configured."

    hdr = f"{'Rule':<16}{'Match Source':<23}{'Pool':<17}Interface"
    sep = "-" * 71
    lines = ["Source NAT rules:", hdr, sep]
    for name, rule in sorted(rules.items()):
        if not isinstance(rule, dict):
            continue
        match_src = rule.get("match_source", "")
        pool = rule.get("then_pool", "")
        iface = rule.get("interface", "")
        lines.append(f"{name:<16}{match_src:<23}{pool:<17}{iface}")
    return "\n".join(lines)


def show_nat_pool(running: dict) -> str:
    """Render 'show security nat pool'."""
    nat = _nat_cfg(running)
    pools: dict[str, Any] = nat.get("pool") or {}
    if not pools:
        return "No NAT pools configured."

    hdr = f"{'Pool':<16}Address"
    sep = "-" * 40
    lines = ["NAT Pools:", hdr, sep]
    for name, pool in sorted(pools.items()):
        if not isinstance(pool, dict):
            continue
        address = pool.get("address", "")
        lines.append(f"{name:<16}{address}")
    return "\n".join(lines)


def show_nat_destination(running: dict) -> str:
    """Render 'show security nat destination'."""
    nat = _nat_cfg(running)
    rules: dict[str, Any] = (nat.get("destination") or {}).get("rule") or {}
    if not rules:
        return "No destination NAT rules configured."

    hdr = (
        f"{'Rule':<16}{'Match Dest':<23}{'Match Port':<12}"
        f"{'Then Dest':<19}Then Port"
    )
    sep = "-" * 83
    lines = ["Destination NAT rules:", hdr, sep]
    for name, rule in sorted(rules.items()):
        if not isinstance(rule, dict):
            continue
        match_dst = rule.get("match_destination", "")
        match_port = str(rule.get("match_destination_port") or "")
        then_dst = rule.get("then_destination", "")
        then_port = str(rule.get("then_destination_port") or "")
        lines.append(
            f"{name:<16}{match_dst:<23}{match_port:<12}{then_dst:<19}{then_port}"
        )
    return "\n".join(lines)


def show_nat_translations() -> str:
    """Render 'show security nat translations' — raw nft table output."""
    try:
        result = subprocess.run(
            ["nft", "list", "table", "inet", "nos_nat"],
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            if "No such file" in result.stderr or "not found" in result.stderr.lower():
                return "NAT table not active (no rules applied)."
            return f"error: {result.stderr.strip()}"
        return result.stdout.strip() or "(empty table)"
    except FileNotFoundError:
        return "error: nft not found in PATH"
