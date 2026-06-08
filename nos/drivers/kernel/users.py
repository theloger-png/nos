"""User management driver — creates/updates/removes Linux user accounts
managed by NOS login configuration.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import warnings
from typing import Any, Dict

from nos.utils.logger import get_logger

log = get_logger(__name__)

_MANAGED_USERS_FILE = "/opt/nos/managed_users.json"
_NOS_GROUP = "nos"
_USERNAME_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_-]{0,31}$")


def _hash_password(plaintext: str) -> str:
    """Return a SHA-512 crypt hash suitable for /etc/shadow."""
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", DeprecationWarning)
        import crypt  # deprecated in 3.12, removed in 3.13 — isolated here
        return crypt.crypt(plaintext, crypt.mksalt(crypt.METHOD_SHA512))


def _user_exists(name: str) -> bool:
    import pwd
    try:
        pwd.getpwnam(name)
        return True
    except KeyError:
        return False


def _run(cmd: list[str]) -> bool:
    """Run *cmd* via sudo; return True on success."""
    try:
        result = subprocess.run(["sudo"] + cmd, capture_output=True, text=True)
        if result.returncode != 0:
            log.error(
                "Command %r failed (rc=%d): %s",
                cmd, result.returncode, result.stderr.strip(),
            )
            return False
        return True
    except FileNotFoundError as exc:
        log.error("Command not found: %s", exc)
        return False


class UserDriver:
    """Reconcile Linux system users with NOS ``system.login`` configuration.

    Users created by NOS are tracked in *_MANAGED_USERS_FILE* so that
    system accounts not owned by NOS are never removed.
    """

    def apply(self, login_config: Dict[str, Any]) -> None:
        """Reconcile system users with *login_config*.

        *login_config* is the dict under ``system.login`` (keys: user → cfg).
        """
        managed = self._load_managed()
        configured: Dict[str, Any] = login_config.get("user") or {}

        for name, user_cfg in configured.items():
            if not _USERNAME_RE.match(name):
                log.warning("Invalid username %r — skipping", name)
                continue

            cfg = user_cfg or {}
            user_class = str(cfg.get("user_class") or "").replace("_", "-")
            auth = cfg.get("authentication") or {}
            hashed_pw: str | None = auth.get("password")

            self._ensure_user(name)
            managed[name] = True

            if hashed_pw:
                self._set_password(name, hashed_pw)

            self._set_groups(name, user_class)

        for name in list(managed.keys()):
            if name not in configured:
                if self._is_logged_in(name):
                    log.warning(
                        "User %r is currently logged in — skipping removal", name
                    )
                    continue
                self._remove_user(name)
                del managed[name]

        self._save_managed(managed)

    # ── per-user operations ────────────────────────────────────────────

    def _ensure_user(self, name: str) -> None:
        if not _user_exists(name):
            log.info("Creating user %r", name)
            _run(["useradd", "-m", "-s", "/bin/bash", name])

    def _set_password(self, name: str, hashed_pw: str) -> None:
        log.info("Setting password for user %r", name)
        entry = f"{name}:{hashed_pw}"
        try:
            result = subprocess.run(
                ["sudo", "chpasswd", "-e"],
                input=entry,
                text=True,
                capture_output=True,
            )
            if result.returncode != 0:
                log.error(
                    "chpasswd failed for %r (rc=%d): %s",
                    name, result.returncode, result.stderr.strip(),
                )
        except FileNotFoundError:
            log.error("chpasswd not found; password not set for %r", name)

    def _set_groups(self, name: str, user_class: str) -> None:
        groups = [_NOS_GROUP]
        if user_class == "super-user":
            groups.append("sudo")
        _run(["usermod", "-aG", ",".join(groups), name])

    def _remove_user(self, name: str) -> None:
        log.info("Removing NOS-managed user %r", name)
        _run(["userdel", "-r", name])

    @staticmethod
    def _is_logged_in(name: str) -> bool:
        try:
            result = subprocess.run(["who"], capture_output=True, text=True)
            for line in result.stdout.splitlines():
                if line.split() and line.split()[0] == name:
                    return True
        except Exception:
            pass
        return False

    # ── managed-users persistence ──────────────────────────────────────

    def _load_managed(self) -> Dict[str, bool]:
        try:
            with open(_MANAGED_USERS_FILE) as fh:
                data = json.load(fh)
            return {k: bool(v) for k, v in data.items()}
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            return {}

    def _save_managed(self, managed: Dict[str, bool]) -> None:
        try:
            os.makedirs(os.path.dirname(_MANAGED_USERS_FILE), exist_ok=True)
            tmp = _MANAGED_USERS_FILE + ".tmp"
            with open(tmp, "w") as fh:
                json.dump(managed, fh, indent=2)
            os.replace(tmp, _MANAGED_USERS_FILE)
        except OSError as exc:
            log.error("Could not save managed users file: %s", exc)
