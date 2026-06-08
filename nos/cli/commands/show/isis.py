"""JunOS-style 'show isis' implementation.

Data sources: vtysh JSON output from FRR isisd.

Command variants:
  show isis
  show isis adjacency [<system-id>]
  show isis database [detail]
  show isis interface [<name>]
  show isis summary
"""
from __future__ import annotations

import json
import logging
from typing import Callable, Optional, TYPE_CHECKING

from nos.cli.parser import resolve_prefix

if TYPE_CHECKING:
    from nos.drivers.frr.client import FRRClient

_LOG = logging.getLogger(__name__)
_NOT_RUNNING = "IS-IS is not running"

_ISIS_SUBCMDS = ["adjacency", "database", "interface", "summary"]


# ── Fetch helpers ──────────────────────────────────────────────────────────────

def _frr_fetch(frr: "FRRClient", cmd: str) -> dict | list:
    """Run a vtysh JSON command; return parsed dict/list or {} on error."""
    try:
        return json.loads(frr.show(cmd))
    except Exception as exc:
        _LOG.debug("FRR command %r failed: %s", cmd, exc)
        return {}


def _frr_fetch_text(frr: "FRRClient", cmd: str) -> str:
    """Run a vtysh command; return raw text or empty string on error."""
    try:
        return frr.show(cmd)
    except Exception as exc:
        _LOG.debug("FRR command %r failed: %s", cmd, exc)
        return ""


# ── 'show isis adjacency' ──────────────────────────────────────────────────────

def _state_str(state: str) -> str:
    mapping = {"Up": "Up", "Down": "Down", "Init": "Init", "Failed": "Failed"}
    return mapping.get(state, state)


def render_adjacency(data: dict, filter_id: str | None = None) -> str:
    """Render adjacency table from 'show isis neighbor json'."""
    # FRR returns {"default": {"adjacencies": [...]}} or {"areas": {...}}
    adjacencies: list[dict] = []
    if isinstance(data, dict):
        for area_data in data.values():
            if isinstance(area_data, dict):
                adjs = area_data.get("adjacencies") or []
                adjacencies.extend(adjs)

    if not adjacencies:
        return "IS-IS instance: default\n\nNo IS-IS adjacencies found.\n"

    lines = ["IS-IS instance: default", ""]
    hdr = f"{'Interface':<12}  {'System ID':<16}  {'State':<6}  {'Hold':<5}  {'SNPA'}"
    lines.append(hdr)
    lines.append("-" * 60)

    for adj in adjacencies:
        sys_id: str = adj.get("sysId") or adj.get("systemId") or "?"
        if filter_id and filter_id not in sys_id:
            continue
        iface: str = adj.get("interface") or "?"
        state: str = _state_str(adj.get("state") or "?")
        hold: int = adj.get("holdtimer") or adj.get("holdTimer") or 0
        snpa: str = adj.get("snpa") or "?"
        lines.append(f"{iface:<12}  {sys_id:<16}  {state:<6}  {hold:<5}  {snpa}")

    return "\n".join(lines) + "\n"


# ── 'show isis database' ───────────────────────────────────────────────────────

def render_database(data: dict, detail: bool = False) -> str:
    """Render IS-IS link-state database from 'show isis database json'."""
    lsps: list[dict] = []
    if isinstance(data, dict):
        for area_data in data.values():
            if isinstance(area_data, dict):
                entries = area_data.get("lsps") or []
                lsps.extend(entries)

    if not lsps:
        return "IS-IS instance: default\n\nIS-IS link-state database is empty.\n"

    lines = ["IS-IS instance: default", ""]

    if detail:
        for lsp in lsps:
            lsp_id: str = lsp.get("lspId") or lsp.get("LSPid") or "?"
            seq: str = str(lsp.get("seqNumber") or lsp.get("seqNum") or "?")
            checksum: str = str(lsp.get("checksum") or "?")
            lifetime: int = lsp.get("remainingLifetime") or lsp.get("lifetime") or 0
            lines.append(f"LSP ID: {lsp_id}")
            lines.append(f"  Sequence:  {seq}")
            lines.append(f"  Checksum:  {checksum}")
            lines.append(f"  Lifetime:  {lifetime}")
            tlvs = lsp.get("tlvs") or []
            for tlv in tlvs:
                lines.append(f"  {tlv}")
            lines.append("")
    else:
        hdr = f"{'LSP ID':<28}  {'Seq':<10}  {'Checksum':<10}  {'Lifetime':<8}  A/L/P/OL"
        lines.append(hdr)
        lines.append("-" * 70)
        for lsp in lsps:
            lsp_id = lsp.get("lspId") or lsp.get("LSPid") or "?"
            seq = hex(lsp.get("seqNumber") or lsp.get("seqNum") or 0)
            checksum = hex(lsp.get("checksum") or 0)
            lifetime = lsp.get("remainingLifetime") or lsp.get("lifetime") or 0
            att = lsp.get("attached") or 0
            lines.append(
                f"{lsp_id:<28}  {seq:<10}  {checksum:<10}  {lifetime:<8}  "
                f"{att}/0/0/0"
            )

    return "\n".join(lines) + "\n"


# ── 'show isis interface' ──────────────────────────────────────────────────────

def render_interface(data: dict, filter_iface: str | None = None) -> str:
    """Render IS-IS interface info from 'show isis interface json'."""
    interfaces: dict[str, dict] = {}
    if isinstance(data, dict):
        for area_data in data.values():
            if isinstance(area_data, dict):
                ifaces = area_data.get("interfaces") or {}
                interfaces.update(ifaces)

    if not interfaces:
        return "IS-IS instance: default\n\nNo IS-IS interfaces configured.\n"

    lines = ["IS-IS instance: default", ""]

    for iface_name in sorted(interfaces):
        if filter_iface and filter_iface != iface_name:
            continue
        ifc = interfaces[iface_name]
        state: str = "Enabled" if ifc.get("state") == "Up" or ifc.get("running") else "Disabled"
        circuit_type: str = ifc.get("circuitType") or ifc.get("type") or "broadcast"
        level: str = ifc.get("level") or "L1L2"
        metric: int = ifc.get("metric") or 10
        adj_count: int = ifc.get("adjacencyCount") or 0

        lines.append(f"Interface: {iface_name}")
        lines.append(f"  State       : {state}")
        lines.append(f"  Circuit type: {circuit_type}")
        lines.append(f"  Level       : {level}")
        lines.append(f"  Metric      : {metric}")
        lines.append(f"  Adjacencies : {adj_count}")
        lines.append("")

    return "\n".join(lines)


# ── 'show isis summary' ────────────────────────────────────────────────────────

def render_summary(data: dict) -> str:
    """Render IS-IS summary from 'show isis summary json'."""
    if not data:
        return f"{_NOT_RUNNING}\n"

    # FRR wraps in instance name key ("default")
    inst = data.get("default") or data
    if not inst:
        return f"{_NOT_RUNNING}\n"

    sys_id: str = inst.get("sysId") or inst.get("systemId") or "?"
    level: str = inst.get("isType") or inst.get("level") or "L1L2"
    net: str = inst.get("net") or inst.get("NET") or "?"
    area: str = inst.get("area") or "?"
    adj_up: int = inst.get("adjacencies") or 0
    lsp_count: int = inst.get("lsps") or 0

    lines = [
        "IS-IS instance: default",
        "",
        f"System ID : {sys_id}",
        f"IS type   : {level}",
        f"NET       : {net}",
        f"Area      : {area}",
        f"Adjacencies up: {adj_up}",
        f"LSPs in database: {lsp_count}",
    ]
    return "\n".join(lines) + "\n"


# ── Entry point ────────────────────────────────────────────────────────────────

def show_isis(
    args: list[str],
    frr: Optional["FRRClient"] = None,
    alias_fn: Optional[Callable[[str], str]] = None,
) -> str:
    """Parse args and produce 'show isis' output."""
    if frr is None:
        return _NOT_RUNNING

    if not args:
        # Default: show adjacency
        data = _frr_fetch(frr, "show isis neighbor json")
        if not data:
            return _NOT_RUNNING
        return render_adjacency(data)

    sub_raw = args[0].lower()
    sub, err = resolve_prefix(sub_raw, _ISIS_SUBCMDS)
    if err:
        return f"error: {err}"
    rest = args[1:]

    if sub == "adjacency":
        data = _frr_fetch(frr, "show isis neighbor json")
        if not data:
            return _NOT_RUNNING
        filter_id = rest[0] if rest else None
        return render_adjacency(data, filter_id=filter_id)

    if sub == "database":
        detail = bool(rest and resolve_prefix(rest[0].lower(), ["detail"])[0] == "detail")
        data = _frr_fetch(frr, "show isis database json")
        if not data:
            return _NOT_RUNNING
        return render_database(data, detail=detail)

    if sub == "interface":
        data = _frr_fetch(frr, "show isis interface json")
        if not data:
            return _NOT_RUNNING
        filter_iface = rest[0] if rest else None
        if alias_fn and filter_iface:
            filter_iface = alias_fn(filter_iface)
        return render_interface(data, filter_iface=filter_iface)

    if sub == "summary":
        data = _frr_fetch(frr, "show isis summary json")
        if not data:
            return _NOT_RUNNING
        return render_summary(data)

    return f"error: unknown 'show isis' sub-command: {sub!r}"
