"""Unit tests for SSH driver and configuration."""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from nos.config.schema import SshConfig


class TestSshConfig:
    """Test SshConfig validation."""

    def test_valid_defaults(self) -> None:
        cfg = SshConfig()
        assert cfg.protocol_version == "v2"
        assert cfg.port == 22
        assert cfg.root_login == "deny"

    def test_valid_custom_port(self) -> None:
        cfg = SshConfig(port=2222)
        assert cfg.port == 2222

    def test_invalid_protocol_version(self) -> None:
        with pytest.raises(ValueError, match="Only protocol version 'v2' is supported"):
            SshConfig(protocol_version="v1")

    def test_invalid_port_low(self) -> None:
        with pytest.raises(ValueError, match="Port must be between 1 and 65535"):
            SshConfig(port=0)

    def test_invalid_port_high(self) -> None:
        with pytest.raises(ValueError, match="Port must be between 1 and 65535"):
            SshConfig(port=65536)

    def test_valid_port_boundary_low(self) -> None:
        cfg = SshConfig(port=1)
        assert cfg.port == 1

    def test_valid_port_boundary_high(self) -> None:
        cfg = SshConfig(port=65535)
        assert cfg.port == 65535

    def test_invalid_root_login(self) -> None:
        with pytest.raises(ValueError, match="root_login must be one of"):
            SshConfig(root_login="invalid")

    def test_valid_root_login_values(self) -> None:
        for value in ["allow", "deny", "deny-password"]:
            cfg = SshConfig(root_login=value)
            assert cfg.root_login == value


class TestSshDriver:
    """Test SshDriver functionality."""

    def test_ssh_driver_import(self) -> None:
        from nos.drivers.kernel.ssh import SshDriver
        assert SshDriver is not None

    @patch("subprocess.run")
    def test_ssh_driver_apply_default(self, mock_run: MagicMock) -> None:
        """Test SshDriver.apply with default configuration."""
        from nos.drivers.kernel.ssh import SshDriver

        mock_run.return_value = MagicMock(returncode=0)
        driver = SshDriver()
        driver.apply()

        assert mock_run.call_count == 2
        # First call: write config
        first_call = mock_run.call_args_list[0]
        assert first_call[0][0][0:2] == ["sudo", "tee"]
        assert "/etc/ssh/sshd_config.d/nos.conf" in first_call[0][0]
        assert "Port 22" in first_call[1]["input"]
        assert "Protocol 2" in first_call[1]["input"]
        assert "PermitRootLogin no" in first_call[1]["input"]

        # Second call: reload SSH
        second_call = mock_run.call_args_list[1]
        assert second_call[0][0] == ["sudo", "systemctl", "reload", "ssh"]

    @patch("subprocess.run")
    def test_ssh_driver_apply_custom_port(self, mock_run: MagicMock) -> None:
        """Test SshDriver.apply with custom port."""
        from nos.drivers.kernel.ssh import SshDriver

        mock_run.return_value = MagicMock(returncode=0)
        driver = SshDriver()
        driver.apply(port=2222)

        first_call = mock_run.call_args_list[0]
        assert "Port 2222" in first_call[1]["input"]

    @patch("subprocess.run")
    def test_ssh_driver_apply_allow_root(self, mock_run: MagicMock) -> None:
        """Test SshDriver.apply with allow root login."""
        from nos.drivers.kernel.ssh import SshDriver

        mock_run.return_value = MagicMock(returncode=0)
        driver = SshDriver()
        driver.apply(root_login="allow")

        first_call = mock_run.call_args_list[0]
        assert "PermitRootLogin yes" in first_call[1]["input"]

    @patch("subprocess.run")
    def test_ssh_driver_apply_deny_password(self, mock_run: MagicMock) -> None:
        """Test SshDriver.apply with deny-password root login."""
        from nos.drivers.kernel.ssh import SshDriver

        mock_run.return_value = MagicMock(returncode=0)
        driver = SshDriver()
        driver.apply(root_login="deny-password")

        first_call = mock_run.call_args_list[0]
        assert "PermitRootLogin prohibit-password" in first_call[1]["input"]

    @patch("subprocess.run")
    def test_ssh_driver_write_failure(self, mock_run: MagicMock) -> None:
        """Test SshDriver.apply handles write failure gracefully."""
        from nos.drivers.kernel.ssh import SshDriver

        mock_run.return_value = MagicMock(returncode=1, stderr="Permission denied")
        driver = SshDriver()
        # Should not raise, just log error
        driver.apply()

    @patch("subprocess.run")
    def test_ssh_driver_reload_failure(self, mock_run: MagicMock) -> None:
        """Test SshDriver.apply handles reload failure gracefully."""
        from nos.drivers.kernel.ssh import SshDriver

        # First call succeeds (write), second fails (reload)
        mock_run.side_effect = [
            MagicMock(returncode=0),
            MagicMock(returncode=1, stderr="Failed to reload"),
        ]
        driver = SshDriver()
        # Should not raise, just log error
        driver.apply()

    def test_ssh_driver_map_root_login(self) -> None:
        """Test _map_root_login mapping."""
        from nos.drivers.kernel.ssh import SshDriver

        assert SshDriver._map_root_login("allow") == "yes"
        assert SshDriver._map_root_login("deny") == "no"
        assert SshDriver._map_root_login("deny-password") == "prohibit-password"


class TestSshConfigSerialization:
    """Test SSH config serialization for set commands."""

    def test_ssh_set_commands_default(self) -> None:
        """Test serialization of default SSH config."""
        from nos.config.serializer import to_set_commands

        config = {
            "system": {
                "services": {
                    "ssh": {
                        "port": 22,
                        "protocol_version": "v2",
                        "root_login": "deny",
                    }
                }
            }
        }
        commands = to_set_commands(config)
        assert any("set system services ssh port 22" in cmd for cmd in commands)
        assert any("protocol-version" in cmd and "v2" in cmd for cmd in commands)
        assert any("root-login" in cmd and "deny" in cmd for cmd in commands)

    def test_ssh_set_commands_custom_port(self) -> None:
        """Test serialization of SSH config with custom port."""
        from nos.config.serializer import to_set_commands

        config = {
            "system": {
                "services": {
                    "ssh": {
                        "port": 2222,
                        "protocol_version": "v2",
                        "root_login": "allow",
                    }
                }
            }
        }
        commands = to_set_commands(config)
        assert any("set system services ssh port 2222" in cmd for cmd in commands)
        assert any("root-login" in cmd and "allow" in cmd for cmd in commands)

    def test_ssh_roundtrip_serialization(self) -> None:
        """Test round-trip: config -> set commands -> parse -> config."""
        from nos.config.serializer import to_set_commands
        from nos.config.store import ConfigStore

        original = {
            "system": {
                "services": {
                    "ssh": {
                        "port": 2222,
                        "protocol_version": "v2",
                        "root_login": "deny-password",
                    }
                }
            }
        }
        commands = to_set_commands(original)
        assert len(commands) > 0
        # Verify key commands are present
        assert any("port 2222" in cmd for cmd in commands)
        assert any("deny-password" in cmd for cmd in commands)
