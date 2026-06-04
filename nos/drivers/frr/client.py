from __future__ import annotations

import logging
import re
import subprocess
from pathlib import Path
from typing import Callable, List, Optional, Set

logger = logging.getLogger(__name__)

_VTYSH_BIN = "/usr/bin/vtysh"
_FRR_CONF = Path("/etc/frr/frr.conf")
_FRR_RELOAD = "/usr/lib/frr/frr-reload.py"
_FRR_DAEMONS = Path("/etc/frr/daemons")

# Maps NOS protocol config key → FRR daemon name in /etc/frr/daemons
_PROTO_TO_DAEMON: dict[str, str] = {
    "bgp": "bgpd",
    "isis": "isisd",
    "ospf": "ospfd",
}


class FRRClientError(Exception):
    """Raised when an FRR operation fails."""


class FRRClient:
    """Client for interacting with FRR via vtysh.

    Per the hard rule in CLAUDE.md, this is the *only* place in NOS that
    invokes vtysh.  All FRR operations must go through this class.

    ``run_fn`` can be injected in tests to replace ``subprocess.run``.
    """

    def __init__(self, run_fn: Optional[Callable] = None) -> None:
        self._run: Callable = run_fn or subprocess.run

    # ------------------------------------------------------------------
    # Configuration
    # ------------------------------------------------------------------

    def send_config(self, commands: List[str]) -> str:
        """Apply a list of configuration commands via vtysh.

        Commands are sent inside a ``configure terminal`` block.  Raises
        ``FRRClientError`` on non-zero exit status.
        """
        vtysh_args = [_VTYSH_BIN, "-c", "configure terminal"]
        for cmd in commands:
            vtysh_args += ["-c", cmd]
        vtysh_args += ["-c", "end"]

        result = self._run(
            vtysh_args,
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            raise FRRClientError(
                f"vtysh config failed (rc={result.returncode}): {result.stderr.strip()}"
            )
        logger.debug("FRR config applied: %s", commands)
        return result.stdout

    def write_frr_conf(self, content: str) -> None:
        """Overwrite /etc/frr/frr.conf with *content* and reload FRR.

        The reload uses ``frr-reload.py`` which performs a warm reload
        (diffs the old config against the new one and applies only the delta).
        Falls back to ``vtysh -f`` when frr-reload.py is unavailable.
        """
        _FRR_CONF.parent.mkdir(parents=True, exist_ok=True)
        try:
            result = self._run(
                ["sudo", "tee", str(_FRR_CONF)],
                input=content.encode(),
                capture_output=True,
            )
            if result.returncode != 0:
                logger.warning(
                    "Could not write %s (rc=%d): %s — FRR config not updated",
                    _FRR_CONF,
                    result.returncode,
                    result.stderr.decode().strip() if isinstance(result.stderr, bytes) else result.stderr.strip(),
                )
                return
        except OSError as exc:
            logger.warning(
                "Cannot write %s: %s — FRR config not updated", _FRR_CONF, exc
            )
            return
        logger.debug("Wrote %d bytes to %s", len(content), _FRR_CONF)
        self._reload()

    # ------------------------------------------------------------------
    # Operational state
    # ------------------------------------------------------------------

    def show(self, command: str) -> str:
        """Execute a vtysh show command and return the output string.

        Raises ``FRRClientError`` on non-zero exit status.
        """
        result = self._run(
            [_VTYSH_BIN, "-c", command],
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            raise FRRClientError(
                f"vtysh show failed (rc={result.returncode}): {result.stderr.strip()}"
            )
        return result.stdout

    # ------------------------------------------------------------------
    # Reload
    # ------------------------------------------------------------------

    def sync_daemons(self, active_protocols: Set[str]) -> None:
        """Enable/disable FRR daemons to match *active_protocols*; restart FRR if changed.

        Reads /etc/frr/daemons, flips bgpd/isisd/ospfd to yes/no as needed,
        writes via ``sudo tee``, then runs ``sudo systemctl restart frr``.
        Both write and restart require sudoers rules (installed by nos-install.sh).

        Permission failures (unwritable file, sudo not configured) are logged as
        warnings and silently skipped.  A failed ``systemctl restart frr`` raises
        FRRClientError because FRR is now in an inconsistent state.
        """
        try:
            content = _FRR_DAEMONS.read_text()
        except OSError as exc:
            logger.warning(
                "Cannot read %s: %s — skipping daemon sync", _FRR_DAEMONS, exc
            )
            return

        wanted: dict[str, str] = {
            daemon: ("yes" if proto in active_protocols else "no")
            for proto, daemon in _PROTO_TO_DAEMON.items()
        }

        lines = content.splitlines()
        new_lines: list[str] = []
        changed = False
        for line in lines:
            matched = False
            for daemon, desired in wanted.items():
                m = re.match(rf"^{re.escape(daemon)}=(yes|no)", line)
                if m:
                    current_val = m.group(1)
                    if current_val != desired:
                        new_lines.append(f"{daemon}={desired}")
                        logger.info(
                            "FRR daemon %s: %s → %s", daemon, current_val, desired
                        )
                        changed = True
                    else:
                        new_lines.append(line)
                    matched = True
                    break
            if not matched:
                new_lines.append(line)

        if not changed:
            return

        new_content = "\n".join(new_lines)
        if content.endswith("\n"):
            new_content += "\n"

        try:
            result = self._run(
                ["sudo", "tee", str(_FRR_DAEMONS)],
                input=new_content,
                capture_output=True,
                text=True,
            )
            if result.returncode != 0:
                logger.warning(
                    "Could not write %s (rc=%d): %s — FRR daemons not updated",
                    _FRR_DAEMONS,
                    result.returncode,
                    result.stderr.strip(),
                )
                return
        except OSError as exc:
            logger.warning(
                "Cannot write %s: %s — FRR daemons not updated", _FRR_DAEMONS, exc
            )
            return

        result = self._run(
            ["sudo", "systemctl", "restart", "frr"],
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            raise FRRClientError(
                f"systemctl restart frr failed (rc={result.returncode}): "
                f"{result.stderr.strip()}"
            )
        logger.info("FRR restarted after daemon config change")

    def _reload(self) -> None:
        reload_bin = Path(_FRR_RELOAD)
        if reload_bin.exists():
            result = self._run(
                ["python3", str(reload_bin), "--reload", str(_FRR_CONF)],
                capture_output=True,
                text=True,
            )
            if result.returncode != 0:
                raise FRRClientError(
                    f"frr-reload.py failed (rc={result.returncode}): {result.stderr.strip()}"
                )
            logger.debug("FRR reloaded via frr-reload.py")
        else:
            # Fallback: load config file directly via vtysh.
            result = self._run(
                [_VTYSH_BIN, "-f", str(_FRR_CONF)],
                capture_output=True,
                text=True,
            )
            if result.returncode != 0:
                raise FRRClientError(
                    f"vtysh -f failed (rc={result.returncode}): {result.stderr.strip()}"
                )
            logger.debug("FRR config loaded via vtysh -f")
