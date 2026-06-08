"""SSH server configuration driver."""
from __future__ import annotations

import subprocess
from typing import Optional

from nos.utils.logger import get_logger

log = get_logger(__name__)


class SshDriver:
    """Manage SSH server configuration via /etc/ssh/sshd_config.d/nos.conf."""

    _CONFIG_PATH = "/etc/ssh/sshd_config.d/nos.conf"

    def apply(
        self,
        port: int = 22,
        protocol_version: str = "v2",
        root_login: str = "deny",
    ) -> None:
        """Apply SSH configuration.

        Args:
            port: SSH listening port (1-65535)
            protocol_version: SSH protocol version (always "v2")
            root_login: Root login policy ("allow", "deny", "deny-password")
        """
        # Disable systemd socket activation on Ubuntu 24.04+
        # socket activation prevents sshd_config port changes.
        if self._is_ssh_socket_active():
            if not self._disable_ssh_socket_activation():
                return

        root_login_sshd = self._map_root_login(root_login)
        config_lines = [
            "# BEGIN NOS MANAGED SSH CONFIG",
            f"Port {port}",
            "Protocol 2",
            f"PermitRootLogin {root_login_sshd}",
            "# END NOS MANAGED SSH CONFIG",
        ]
        config_content = "\n".join(config_lines) + "\n"

        try:
            cmd = ["sudo", "tee", self._CONFIG_PATH]
            result = subprocess.run(
                cmd,
                input=config_content,
                text=True,
                capture_output=True,
            )
            if result.returncode != 0:
                log.error(
                    "Failed to write SSH config (rc=%d): %s",
                    result.returncode,
                    result.stderr.strip(),
                )
                return
            log.info("SSH config written to %s", self._CONFIG_PATH)
        except Exception as exc:
            log.error("Error writing SSH config: %s", exc)
            return

        try:
            result = subprocess.run(
                ["sudo", "systemctl", "restart", "ssh"],
                capture_output=True,
                text=True,
            )
            if result.returncode != 0:
                log.error(
                    "Failed to restart SSH (rc=%d): %s",
                    result.returncode,
                    result.stderr.strip(),
                )
            else:
                log.info("SSH service restarted")
        except Exception as exc:
            log.error("Error restarting SSH service: %s", exc)

    def _is_ssh_socket_active(self) -> bool:
        """Check if ssh.socket is active (Ubuntu 24.04+ socket activation)."""
        try:
            result = subprocess.run(
                ["sudo", "systemctl", "is-active", "ssh.socket"],
                capture_output=True,
                text=True,
            )
            return result.returncode == 0
        except Exception as exc:
            log.debug("Error checking ssh.socket status: %s", exc)
            return False

    def _disable_ssh_socket_activation(self) -> bool:
        """Disable systemd socket activation and enable classic sshd mode.

        Returns:
            True if successful, False if any step fails.
        """
        commands = [
            (["sudo", "systemctl", "disable", "ssh.socket"], "disable ssh.socket"),
            (["sudo", "systemctl", "stop", "ssh.socket"], "stop ssh.socket"),
            (["sudo", "systemctl", "enable", "ssh"], "enable ssh"),
        ]

        for cmd, desc in commands:
            try:
                result = subprocess.run(
                    cmd,
                    capture_output=True,
                    text=True,
                )
                if result.returncode != 0:
                    log.error(
                        "Failed to %s (rc=%d): %s",
                        desc,
                        result.returncode,
                        result.stderr.strip(),
                    )
                    return False
                log.info("Successfully %s", desc)
            except Exception as exc:
                log.error("Error during %s: %s", desc, exc)
                return False

        return True

    @staticmethod
    def _map_root_login(nos_value: str) -> str:
        """Map NOS root_login value to sshd_config value."""
        mapping = {
            "allow": "yes",
            "deny": "no",
            "deny-password": "prohibit-password",
        }
        return mapping.get(nos_value, "no")
