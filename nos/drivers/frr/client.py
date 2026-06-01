from __future__ import annotations

import logging
import subprocess
from pathlib import Path
from typing import Callable, List, Optional

logger = logging.getLogger(__name__)

_VTYSH_BIN = "/usr/bin/vtysh"
_FRR_CONF = Path("/etc/frr/frr.conf")
_FRR_RELOAD = "/usr/lib/frr/frr-reload.py"


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
        _FRR_CONF.write_text(content)
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
