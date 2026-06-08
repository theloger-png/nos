"""Show commands for system login users."""
from __future__ import annotations

from typing import Any, Dict


def show_login(login_config: Dict[str, Any]) -> str:
    """Render login users as a formatted table."""
    users: Dict[str, Any] = login_config.get("user") or {}
    if not users:
        return "No login users configured."

    header = f"{'User':<16}{'Class':<16}Status"
    sep = "-" * 47
    lines = ["Login users:", header, sep]
    for name in sorted(users):
        cfg = users[name] or {}
        raw_class = cfg.get("class") or "(none)"
        user_class = str(raw_class).replace("_", "-")
        lines.append(f"{name:<16}{user_class:<16}active")
    return "\n".join(lines)
