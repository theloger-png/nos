from __future__ import annotations

from typing import Any


def _k2j(key: str) -> str:
    return str(key).replace("_", "-")


def _render_block(key: str, value: Any, sign: str, indent: int) -> list[str]:
    pad = "    " * indent
    jkey = _k2j(key)

    if value is None:
        return []
    if isinstance(value, bool):
        return [f"{sign}   {pad}{jkey};"] if value else []
    if isinstance(value, dict):
        if not value:
            return [f"{sign}   {pad}{jkey};"]
        lines = [f"{sign}   {pad}{jkey} {{"]
        for k, v in sorted(value.items(), key=lambda x: str(x[0])):
            lines.extend(_render_block(k, v, sign, indent + 1))
        lines.append(f"{sign}   {pad}}}")
        return lines
    if isinstance(value, list):
        lines: list[str] = []
        for item in value:
            if isinstance(item, str):
                lines.append(f"{sign}   {pad}{jkey} {item};")
            else:
                lines.extend(_render_block(key, item, sign, indent))
        return lines
    s = str(value)
    val_str = f'"{s}"' if isinstance(value, str) and (" " in s or not s) else s
    return [f"{sign}   {pad}{jkey} {val_str};"]


def _compare(old: Any, new: Any, path: tuple, chunks: list) -> None:
    if not isinstance(old, dict) or not isinstance(new, dict):
        return

    all_keys = sorted(set(old) | set(new), key=str)
    local_lines: list[str] = []

    for key in all_keys:
        if key not in old:
            local_lines.extend(_render_block(key, new[key], "+", 0))
        elif key not in new:
            local_lines.extend(_render_block(key, old[key], "-", 0))
        elif old[key] != new[key]:
            if isinstance(old[key], dict) and isinstance(new[key], dict):
                _compare(old[key], new[key], path + (key,), chunks)
            else:
                local_lines.extend(_render_block(key, old[key], "-", 0))
                local_lines.extend(_render_block(key, new[key], "+", 0))

    if local_lines:
        path_str = " ".join(_k2j(str(p)) for p in path)
        header = f"[edit {path_str}]" if path_str else "[edit]"
        chunks.append((header, local_lines))


def diff(old: dict, new: dict) -> str:
    """Return JunOS-style diff between old (running) and new (candidate).

    Lines prefixed with + are in candidate but not running.
    Lines prefixed with - are in running but not candidate.
    Returns empty string when configs are identical.
    """
    chunks: list = []
    _compare(old, new, (), chunks)
    if not chunks:
        return ""
    parts: list[str] = []
    for header, lines in chunks:
        parts.append(header)
        parts.extend(lines)
        parts.append("")
    return "\n".join(parts).rstrip()
