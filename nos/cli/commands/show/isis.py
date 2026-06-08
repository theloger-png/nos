"""JunOS-style 'show isis' implementation.

Data sources: vtysh JSON output from FRR isisd (FRR 8.x format).

FRR 8.x JSON structure:
  show isis interface json → {"areas": [{"area": "default", "circuits": [...]}]}
  show isis neighbor json  → {"areas": [{"area": "default", "circuits": [...]}]}
  show isis database json  → {"areas": [{"area": {"name": "default"}, "levels": [...]}]}
  show isis summary json   → {"vrf": "...", "system-id": "...", "areas": [...]}

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
    try:
        return json.loads(frr.show(cmd))
    except Exception as exc:
        _LOG.debug("FRR command %r failed: %s", cmd, exc)
        return {}


def _get_areas(data: dict) -> list[dict]:
    """Return the list of area dicts from a top-level FRR ISIS JSON response."""
    areas = data.get("areas") or []
    return areas if isinstance(areas, list) else []


# ── 'show isis interface' ──────────────────────────────────────────────────────

def render_interface(data: dict, filter_iface: str | None = None) -> str:
    """Render IS-IS interface info from 'show isis interface json'.

    FRR 8.x structure:
      {"areas": [{"area": "default", "circuits": [
        {"circuit": 0, "interface": {"name": "ens34", "state": "Up", ...}}
      ]}]}
    """
    circuits: list[dict] = []
    for area in _get_areas(data):
        for c in area.get("circuits") or []:
            ifc = c.get("interface")
            if isinstance(ifc, dict) and ifc.get("name"):
                circuits.append(ifc)

    if not circuits:
        return "IS-IS instance: default\n\nNo IS-IS interfaces configured.\n"

    lines = ["IS-IS instance: default", ""]
    hdr = f"{'Interface':<12}  {'CircID':<8}  {'State':<6}  {'Type':<8}  Level"
    lines.append(hdr)
    lines.append("-" * 50)

    for ifc in circuits:
        name: str = ifc.get("name", "?")
        if filter_iface and filter_iface != name:
            continue
        cid: str = ifc.get("circuit-id", "?")
        state: str = ifc.get("state", "?")
        typ: str = ifc.get("type", "?")
        level: str = ifc.get("level", "?")
        lines.append(f"{name:<12}  {cid:<8}  {state:<6}  {typ:<8}  {level}")

    if len(lines) == 3:  # only header + separator, nothing matched filter
        lines.append(f"No IS-IS interface matching {filter_iface!r}.")

    return "\n".join(lines) + "\n"


# ── 'show isis adjacency' ──────────────────────────────────────────────────────

def render_adjacency(data: dict, filter_id: str | None = None) -> str:
    """Render adjacency table from 'show isis neighbor json'.

    FRR 8.x structure:
      {"areas": [{"area": "default", "circuits": [
        {"circuit": 0, "adjacencies": [{"sysId": "...", ...}]}
      ]}]}
    """
    adjacencies: list[tuple[str, dict]] = []  # (interface_name, adj_dict)
    for area in _get_areas(data):
        for c in area.get("circuits") or []:
            ifc_name = (c.get("interface") or {}).get("name") or "?"
            for adj in c.get("adjacencies") or []:
                if isinstance(adj, dict):
                    adjacencies.append((ifc_name, adj))

    if not adjacencies:
        return "IS-IS instance: default\n\nNo IS-IS adjacencies found.\n"

    lines = ["IS-IS instance: default", ""]
    hdr = f"{'Interface':<12}  {'System ID':<20}  {'State':<6}  {'Hold':<5}  SNPA"
    lines.append(hdr)
    lines.append("-" * 65)

    for ifc_name, adj in adjacencies:
        sys_id: str = adj.get("sysId") or adj.get("systemId") or "?"
        if filter_id and filter_id not in sys_id:
            continue
        state: str = adj.get("state") or "?"
        hold: int = adj.get("holdtimer") or adj.get("holdTimer") or 0
        snpa: str = adj.get("snpa") or "?"
        lines.append(f"{ifc_name:<12}  {sys_id:<20}  {state:<6}  {hold:<5}  {snpa}")

    return "\n".join(lines) + "\n"


# ── 'show isis database' ───────────────────────────────────────────────────────

def render_database(data: dict, detail: bool = False) -> str:
    """Render IS-IS link-state database from 'show isis database json'.

    FRR 8.x structure:
      {"areas": [{"area": {"name": "default"}, "levels": [
        {"id": 1, "lsp": {"id": "nos-dev.00-00"}, "seq-number": "0x00000002", ...}
      ]}]}
    """
    entries: list[tuple[int, dict]] = []  # (level, lsp_dict)
    for area in _get_areas(data):
        for lvl in area.get("levels") or []:
            level_id: int = lvl.get("id", 0)
            lsp = lvl.get("lsp")
            if isinstance(lsp, dict):
                lsp["_level"] = level_id
                lsp["_pdu_len"] = lvl.get("pdu-len")
                lsp["_seq"] = lvl.get("seq-number")
                lsp["_chksum"] = lvl.get("chksum")
                lsp["_holdtime"] = lvl.get("holdtime")
                lsp["_att_p_ol"] = lvl.get("att-p-ol", "0/0/0")
                entries.append((level_id, lsp))

    if not entries:
        return "IS-IS instance: default\n\nIS-IS link-state database is empty.\n"

    lines = ["IS-IS instance: default", ""]

    for level_id, lsp in entries:
        lsp_id: str = lsp.get("id", "?")
        seq: str = lsp.get("_seq") or "?"
        chksum: str = lsp.get("_chksum") or "?"
        holdtime = lsp.get("_holdtime") or 0
        att_p_ol: str = lsp.get("_att_p_ol", "0/0/0")
        own: str = lsp.get("own", " ")

        if detail:
            lines.append(f"IS-IS Level-{level_id} Link State Database:")
            lines.append(f"  LSP ID:    {lsp_id}")
            lines.append(f"  Sequence:  {seq}")
            lines.append(f"  Checksum:  {chksum}")
            lines.append(f"  Lifetime:  {holdtime}")
            lines.append(f"  A/P/OL:    {att_p_ol}")
            lines.append(f"  Flags:     {own.strip()}")
            lines.append("")
        else:
            level_header = f"IS-IS Level-{level_id} Link State Database:"
            if level_header not in lines:
                lines.append(level_header)
                hdr = f"  {'LSP ID':<26}  {'Seq':<12}  {'Checksum':<10}  {'Holdtime':<8}  A/P/OL"
                lines.append(hdr)
                lines.append("  " + "-" * 70)
            own_marker = own if own.strip() else " "
            lines.append(
                f"  {own_marker}{lsp_id:<25}  {seq:<12}  {chksum:<10}  {holdtime:<8}  {att_p_ol}"
            )

    return "\n".join(lines) + "\n"


# ── 'show isis summary' ────────────────────────────────────────────────────────

def render_summary(data: dict) -> str:
    """Render IS-IS summary from 'show isis summary json'.

    FRR 8.x structure:
      {"vrf": "default", "system-id": "...", "areas": [{"area": "default", "net": "..."}]}
    """
    if not data:
        return f"{_NOT_RUNNING}\n"

    sys_id: str = data.get("system-id") or "?"
    uptime: str = data.get("up-time") or "?"
    n_areas: int = data.get("number-areas") or 0
    vrf: str = data.get("vrf") or "default"

    lines = [
        "IS-IS instance: default",
        "",
        f"VRF        : {vrf}",
        f"System ID  : {sys_id}",
        f"Up time    : {uptime}",
        f"Areas      : {n_areas}",
    ]

    for area in _get_areas(data):
        area_name: str = area.get("area") or "?"
        net: str = area.get("net") or "?"
        lines.append("")
        lines.append(f"Area: {area_name}")
        lines.append(f"  NET: {net}")
        for lvl in area.get("levels") or []:
            lid = lvl.get("id", "?")
            last_spf = lvl.get("last-run-elapsed") or "never"
            lines.append(f"  Level {lid}: last SPF {last_spf} ago")

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
        data = _frr_fetch(frr, "show isis interface json")
        if not data:
            return _NOT_RUNNING
        return render_interface(data)

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
        return render_interface(data, filter_iface=filter_iface)

    if sub == "summary":
        data = _frr_fetch(frr, "show isis summary json")
        if not data:
            return _NOT_RUNNING
        return render_summary(data)

    return f"error: unknown 'show isis' sub-command: {sub!r}"
