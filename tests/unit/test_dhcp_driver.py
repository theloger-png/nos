"""Unit tests for nos.drivers.dhcp.dnsmasq.DnsmasqDriver."""
from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from nos.drivers.dhcp.dnsmasq import DnsmasqDriver


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture()
def tmp_driver(tmp_path: Path) -> DnsmasqDriver:
    """Return a DnsmasqDriver wired to tmp_path directories."""
    conf_dir = tmp_path / "dnsmasq.d"
    conf_dir.mkdir()
    server_leases = tmp_path / "dnsmasq.leases"
    client_leases = tmp_path / "dhclient.leases"
    pid_dir = tmp_path / "run"
    pid_dir.mkdir()
    return DnsmasqDriver(
        conf_dir=conf_dir,
        server_leases_file=server_leases,
        client_leases_file=client_leases,
        pidfile_dir=pid_dir,
    )


def _make_config(
    pools: dict,
    iface_pools: dict,
) -> dict:
    """Build a minimal NOS config dict with dhcp-local-server entries."""
    return {
        "system": {
            "services": {
                "dhcp_local_server": {
                    "pool": pools,
                    "interface": iface_pools,
                }
            }
        }
    }


# ---------------------------------------------------------------------------
# Config generation — single pool
# ---------------------------------------------------------------------------

def test_single_pool_generates_conf_file(tmp_driver: DnsmasqDriver) -> None:
    config = _make_config(
        pools={
            "mypool": {
                "range": {"low": "10.0.0.100", "high": "10.0.0.200"},
                "gateway": "10.0.0.1",
            }
        },
        iface_pools={"eth0": {"pool": ["mypool"]}},
    )
    with patch.object(tmp_driver, "_reload_dnsmasq"):
        tmp_driver.apply(config)

    conf = tmp_driver._conf_dir / "nos-eth0-mypool.conf"
    assert conf.exists(), "Config file should be created"
    text = conf.read_text()
    assert "dhcp-range=eth0,10.0.0.100,10.0.0.200" in text
    assert "dhcp-option=eth0,3,10.0.0.1" in text


def test_single_pool_with_dns_server(tmp_driver: DnsmasqDriver) -> None:
    config = _make_config(
        pools={
            "mypool": {
                "range": {"low": "192.168.1.50", "high": "192.168.1.100"},
                "gateway": "192.168.1.1",
                "dns_server": "8.8.8.8",
            }
        },
        iface_pools={"eth0": {"pool": ["mypool"]}},
    )
    with patch.object(tmp_driver, "_reload_dnsmasq"):
        tmp_driver.apply(config)

    text = (tmp_driver._conf_dir / "nos-eth0-mypool.conf").read_text()
    assert "dhcp-option=eth0,6,8.8.8.8" in text


def test_single_pool_without_dns_server(tmp_driver: DnsmasqDriver) -> None:
    config = _make_config(
        pools={
            "mypool": {
                "range": {"low": "192.168.1.50", "high": "192.168.1.100"},
                "gateway": "192.168.1.1",
            }
        },
        iface_pools={"eth0": {"pool": ["mypool"]}},
    )
    with patch.object(tmp_driver, "_reload_dnsmasq"):
        tmp_driver.apply(config)

    text = (tmp_driver._conf_dir / "nos-eth0-mypool.conf").read_text()
    assert "dhcp-option=eth0,6" not in text


# ---------------------------------------------------------------------------
# Config generation — multiple pools on same interface
# ---------------------------------------------------------------------------

def test_multiple_pools_same_interface(tmp_driver: DnsmasqDriver) -> None:
    config = _make_config(
        pools={
            "pool-a": {
                "range": {"low": "10.1.0.10", "high": "10.1.0.50"},
                "gateway": "10.1.0.1",
            },
            "pool-b": {
                "range": {"low": "10.2.0.10", "high": "10.2.0.50"},
                "gateway": "10.2.0.1",
                "dns_server": "1.1.1.1",
            },
        },
        iface_pools={"eth1": {"pool": ["pool-a", "pool-b"]}},
    )
    with patch.object(tmp_driver, "_reload_dnsmasq"):
        tmp_driver.apply(config)

    conf_a = tmp_driver._conf_dir / "nos-eth1-pool-a.conf"
    conf_b = tmp_driver._conf_dir / "nos-eth1-pool-b.conf"
    assert conf_a.exists()
    assert conf_b.exists()
    assert "dhcp-range=eth1,10.1.0.10,10.1.0.50" in conf_a.read_text()
    assert "dhcp-range=eth1,10.2.0.10,10.2.0.50" in conf_b.read_text()
    assert "dhcp-option=eth1,6,1.1.1.1" in conf_b.read_text()
    assert "dhcp-option=eth1,6" not in conf_a.read_text()


def test_multiple_interfaces(tmp_driver: DnsmasqDriver) -> None:
    config = _make_config(
        pools={
            "lan-pool": {
                "range": {"low": "192.168.0.10", "high": "192.168.0.254"},
                "gateway": "192.168.0.1",
            },
            "mgmt-pool": {
                "range": {"low": "10.99.0.10", "high": "10.99.0.50"},
                "gateway": "10.99.0.1",
            },
        },
        iface_pools={
            "eth0": {"pool": ["lan-pool"]},
            "eth1": {"pool": ["mgmt-pool"]},
        },
    )
    with patch.object(tmp_driver, "_reload_dnsmasq"):
        tmp_driver.apply(config)

    assert (tmp_driver._conf_dir / "nos-eth0-lan-pool.conf").exists()
    assert (tmp_driver._conf_dir / "nos-eth1-mgmt-pool.conf").exists()


# ---------------------------------------------------------------------------
# No config → cleanup
# ---------------------------------------------------------------------------

def test_no_config_removes_existing_files(tmp_driver: DnsmasqDriver) -> None:
    # Pre-create some nos-*.conf files to simulate leftover config.
    (tmp_driver._conf_dir / "nos-eth0-mypool.conf").write_text("old config\n")
    (tmp_driver._conf_dir / "nos-eth1-pool2.conf").write_text("old config2\n")
    # nos-base.conf should NOT be removed (glob is nos-[!b]*.conf).
    base = tmp_driver._conf_dir / "nos-base.conf"
    base.write_text("no-resolv\n")

    with patch.object(tmp_driver, "_reload_dnsmasq"):
        tmp_driver.apply({})

    assert not (tmp_driver._conf_dir / "nos-eth0-mypool.conf").exists()
    assert not (tmp_driver._conf_dir / "nos-eth1-pool2.conf").exists()
    assert base.exists(), "nos-base.conf must not be removed"


def test_no_config_calls_reload(tmp_driver: DnsmasqDriver) -> None:
    mock_reload = MagicMock()
    with patch.object(tmp_driver, "_reload_dnsmasq", mock_reload):
        tmp_driver.apply({})
    mock_reload.assert_called_once()


# ---------------------------------------------------------------------------
# Server lease file parsing
# ---------------------------------------------------------------------------

_SAMPLE_LEASES = """\
1735689600 aa:bb:cc:dd:ee:01 10.0.0.101 host1 *
1735693200 aa:bb:cc:dd:ee:02 10.0.0.102 host2 client-2
1735696800 aa:bb:cc:dd:ee:03 192.168.1.50 host3 *
"""


def test_parse_server_leases_all(tmp_driver: DnsmasqDriver) -> None:
    tmp_driver._server_leases_file.write_text(_SAMPLE_LEASES)
    leases = tmp_driver.parse_server_leases()
    assert len(leases) == 3
    assert leases[0]["ip"] == "10.0.0.101"
    assert leases[0]["mac"] == "aa:bb:cc:dd:ee:01"
    assert leases[0]["hostname"] == "host1"
    assert leases[1]["client_id"] == "client-2"


def test_parse_server_leases_empty_file(tmp_driver: DnsmasqDriver) -> None:
    tmp_driver._server_leases_file.write_text("")
    assert tmp_driver.parse_server_leases() == []


def test_parse_server_leases_missing_file(tmp_driver: DnsmasqDriver) -> None:
    # File was never created
    assert tmp_driver.parse_server_leases() == []


def test_parse_server_leases_iface_filter(tmp_driver: DnsmasqDriver) -> None:
    # Write a pool conf so the driver knows which IPs belong to eth0.
    (tmp_driver._conf_dir / "nos-eth0-mypool.conf").write_text(
        "dhcp-range=eth0,10.0.0.100,10.0.0.200\n"
    )
    tmp_driver._server_leases_file.write_text(_SAMPLE_LEASES)
    leases = tmp_driver.parse_server_leases(iface_filter="eth0")
    # 10.0.0.101 and 10.0.0.102 fall in range; 192.168.1.50 does not.
    assert len(leases) == 2
    assert all(l["ip"].startswith("10.0.0.") for l in leases)


# ---------------------------------------------------------------------------
# Client lease file parsing
# ---------------------------------------------------------------------------

_SAMPLE_CLIENT_LEASES = """\
lease {
  interface "eth0";
  fixed-address 192.168.100.50;
  option subnet-mask 255.255.255.0;
  option routers 192.168.100.1;
  expire 4 2026/01/15 12:00:00;
}
lease {
  interface "eth0";
  fixed-address 192.168.100.55;
  option subnet-mask 255.255.255.0;
  option routers 192.168.100.1;
  expire 5 2026/01/16 08:00:00;
}
lease {
  interface "eth1";
  fixed-address 10.0.0.20;
  option subnet-mask 255.0.0.0;
  option routers 10.0.0.1;
  expire 6 2026/01/17 10:00:00;
}
"""


def test_parse_client_leases_basic(tmp_driver: DnsmasqDriver) -> None:
    tmp_driver._client_leases_file.write_text(_SAMPLE_CLIENT_LEASES)
    leases = tmp_driver.parse_client_leases()
    # Only last lease per interface is kept.
    assert len(leases) == 2
    by_iface = {l["iface"]: l for l in leases}
    assert by_iface["eth0"]["ip"] == "192.168.100.55"
    assert by_iface["eth1"]["ip"] == "10.0.0.20"
    assert by_iface["eth1"]["gateway"] == "10.0.0.1"


def test_parse_client_leases_missing_file(tmp_driver: DnsmasqDriver) -> None:
    assert tmp_driver.parse_client_leases() == []


def test_parse_client_leases_empty_file(tmp_driver: DnsmasqDriver) -> None:
    tmp_driver._client_leases_file.write_text("")
    assert tmp_driver.parse_client_leases() == []


# ---------------------------------------------------------------------------
# Server statistics
# ---------------------------------------------------------------------------

def test_server_statistics_counts_leases(tmp_driver: DnsmasqDriver) -> None:
    tmp_driver._server_leases_file.write_text(_SAMPLE_LEASES)
    config = _make_config(
        pools={
            "mypool": {
                "range": {"low": "10.0.0.100", "high": "10.0.0.200"},
                "gateway": "10.0.0.1",
            }
        },
        iface_pools={"eth0": {"pool": ["mypool"]}},
    )
    stats = tmp_driver.server_statistics(config)
    assert len(stats) == 1
    assert stats[0]["pool"] == "mypool"
    assert stats[0]["active"] == 2  # 10.0.0.101 and 10.0.0.102 in range


def test_server_statistics_no_config(tmp_driver: DnsmasqDriver) -> None:
    stats = tmp_driver.server_statistics({})
    assert stats == []


# ---------------------------------------------------------------------------
# DhcpInterfaceConfig coercion (schema level)
# ---------------------------------------------------------------------------

def test_dhcp_interface_config_pool_coercion() -> None:
    """pool accepts dict, list, or string and normalises to List[str]."""
    from nos.config.schema import DhcpInterfaceConfig

    # dict form (from_set_commands produces this)
    cfg = DhcpInterfaceConfig.model_validate({"pool": {"mypool": True}})
    assert cfg.pool == ["mypool"]

    # list form
    cfg2 = DhcpInterfaceConfig.model_validate({"pool": ["p1", "p2"]})
    assert cfg2.pool == ["p1", "p2"]

    # string form
    cfg3 = DhcpInterfaceConfig.model_validate({"pool": "singlepool"})
    assert cfg3.pool == ["singlepool"]


def test_family_inet_dhcp_xor_static() -> None:
    """family inet dhcp and static address are mutually exclusive."""
    from nos.config.schema import FamilyInet
    import pytest

    with pytest.raises(Exception):
        FamilyInet.model_validate(
            {"dhcp": True, "address": {"192.168.1.1/24": {}}}
        )


# ---------------------------------------------------------------------------
# DHCP Client
# ---------------------------------------------------------------------------

def test_apply_client_unit_dhcp(tmp_driver: DnsmasqDriver) -> None:
    """apply_client should detect dhcp on interface units (e.g., irb.101)."""
    config = {
        "interfaces": {
            "irb": {
                "unit": {
                    "101": {
                        "family_inet": {"dhcp": True}
                    }
                }
            }
        }
    }
    mock_start = MagicMock()
    with patch.object(tmp_driver, "_start_dhclient", mock_start):
        with patch.object(tmp_driver, "_dhclient_running", return_value=False):
            tmp_driver.apply_client(config)

    # Check that _start_dhclient was called with the correct interface name
    mock_start.assert_called_once()
    call_args = mock_start.call_args
    assert call_args[0][0] == "irb.101"


def test_apply_client_main_and_unit_dhcp(tmp_driver: DnsmasqDriver) -> None:
    """apply_client should handle both main interface and unit dhcp."""
    config = {
        "interfaces": {
            "eth0": {
                "family_inet": {"dhcp": True},
                "unit": {
                    "100": {
                        "family_inet": {"dhcp": True}
                    }
                }
            }
        }
    }
    mock_start = MagicMock()
    with patch.object(tmp_driver, "_start_dhclient", mock_start):
        with patch.object(tmp_driver, "_dhclient_running", return_value=False):
            tmp_driver.apply_client(config)

    # Should be called for both eth0 and eth0.100
    assert mock_start.call_count == 2
    calls = [call[0][0] for call in mock_start.call_args_list]
    assert "eth0" in calls
    assert "eth0.100" in calls


def test_apply_client_multiple_units(tmp_driver: DnsmasqDriver) -> None:
    """apply_client should handle multiple units with dhcp."""
    config = {
        "interfaces": {
            "irb": {
                "unit": {
                    "101": {"family_inet": {"dhcp": True}},
                    "102": {"family_inet": {"dhcp": True}},
                }
            }
        }
    }
    mock_start = MagicMock()
    with patch.object(tmp_driver, "_start_dhclient", mock_start):
        with patch.object(tmp_driver, "_dhclient_running", return_value=False):
            tmp_driver.apply_client(config)

    # Should be called for both irb.101 and irb.102
    assert mock_start.call_count == 2
    calls = [call[0][0] for call in mock_start.call_args_list]
    assert "irb.101" in calls
    assert "irb.102" in calls


def test_apply_client_underscore_conversion(tmp_driver: DnsmasqDriver) -> None:
    """apply_client should convert underscores to hyphens in interface names."""
    config = {
        "interfaces": {
            "bond_0": {
                "family_inet": {"dhcp": True}
            },
            "irb": {
                "unit": {
                    "101": {
                        "family_inet": {"dhcp": True}
                    }
                }
            }
        }
    }
    mock_start = MagicMock()
    with patch.object(tmp_driver, "_start_dhclient", mock_start):
        with patch.object(tmp_driver, "_dhclient_running", return_value=False):
            tmp_driver.apply_client(config)

    # Should convert bond_0 to bond-0, and irb.101 should be as-is
    calls = [call[0][0] for call in mock_start.call_args_list]
    assert "bond-0" in calls
    assert "irb.101" in calls


# ---------------------------------------------------------------------------
# Interface alias translation
# ---------------------------------------------------------------------------

def test_apply_with_alias_translation(tmp_driver: DnsmasqDriver) -> None:
    """apply() should translate NOS interface names to kernel names in config."""
    # Create a mock alias map: et1 -> ens34, et0 -> ens33
    alias_map = {"ens34": "et1", "ens33": "et0"}

    config = _make_config(
        pools={
            "pool1": {
                "range": {"low": "10.0.0.100", "high": "10.0.0.200"},
                "gateway": "10.0.0.1",
            }
        },
        iface_pools={"et1": {"pool": ["pool1"]}},
    )

    with patch.object(tmp_driver, "_reload_dnsmasq"):
        with patch("nos.drivers.dhcp.dnsmasq.get_alias_map", return_value=alias_map):
            tmp_driver.apply(config)

    # Config filename should have NOS name (et1)
    conf = tmp_driver._conf_dir / "nos-et1-pool1.conf"
    assert conf.exists(), "Config file should be created with NOS name"

    # Config content should have kernel name (ens34)
    text = conf.read_text()
    assert "dhcp-range=ens34,10.0.0.100,10.0.0.200" in text
    assert "dhcp-option=ens34,3,10.0.0.1" in text


def test_apply_with_alias_translation_subinterface(tmp_driver: DnsmasqDriver) -> None:
    """apply() should handle NOS subinterface names (e.g., et1.101 -> ens34.101)."""
    alias_map = {"ens34": "et1", "ens33": "et0"}

    config = _make_config(
        pools={
            "vlan_pool": {
                "range": {"low": "10.1.0.10", "high": "10.1.0.50"},
                "gateway": "10.1.0.1",
                "dns_server": "8.8.8.8",
            }
        },
        iface_pools={"et1.101": {"pool": ["vlan_pool"]}},
    )

    with patch.object(tmp_driver, "_reload_dnsmasq"):
        with patch("nos.drivers.dhcp.dnsmasq.get_alias_map", return_value=alias_map):
            tmp_driver.apply(config)

    # Config filename should have NOS name (et1.101)
    conf = tmp_driver._conf_dir / "nos-et1.101-vlan_pool.conf"
    assert conf.exists(), "Config file should be created with NOS name"

    # Config content should have kernel name (ens34.101)
    text = conf.read_text()
    assert "dhcp-range=ens34.101,10.1.0.10,10.1.0.50" in text
    assert "dhcp-option=ens34.101,3,10.1.0.1" in text
    assert "dhcp-option=ens34.101,6,8.8.8.8" in text


def test_apply_with_unknown_interface_no_alias(tmp_driver: DnsmasqDriver) -> None:
    """apply() should use interface name as-is if no alias mapping exists."""
    alias_map = {"ens34": "et1"}  # Only et1 is mapped

    config = _make_config(
        pools={
            "pool1": {
                "range": {"low": "10.0.0.100", "high": "10.0.0.200"},
                "gateway": "10.0.0.1",
            }
        },
        iface_pools={"eth0": {"pool": ["pool1"]}},  # eth0 has no alias
    )

    with patch.object(tmp_driver, "_reload_dnsmasq"):
        with patch("nos.drivers.dhcp.dnsmasq.get_alias_map", return_value=alias_map):
            tmp_driver.apply(config)

    # eth0 should be used as-is
    conf = tmp_driver._conf_dir / "nos-eth0-pool1.conf"
    assert conf.exists()
    text = conf.read_text()
    assert "dhcp-range=eth0,10.0.0.100,10.0.0.200" in text


# ---------------------------------------------------------------------------
# sudo command execution
# ---------------------------------------------------------------------------

def test_start_dhclient_uses_sudo(tmp_driver: DnsmasqDriver) -> None:
    """_start_dhclient should run dhclient via sudo."""
    pidfile = tmp_driver._pidfile_dir / "test.pid"

    with patch("subprocess.Popen") as mock_popen:
        tmp_driver._start_dhclient("eth0", pidfile)

    mock_popen.assert_called_once()
    cmd = mock_popen.call_args[0][0]
    assert cmd[0] == "sudo"
    assert cmd[1] == "dhclient"
    assert "-pf" in cmd
    assert str(pidfile) in cmd
    assert "eth0" in cmd


def test_stop_dhclient_uses_sudo_kill(tmp_driver: DnsmasqDriver) -> None:
    """_stop_dhclient should use sudo kill to terminate dhclient."""
    pidfile = tmp_driver._pidfile_dir / "dhclient-eth0.pid"
    pidfile.write_text("12345")

    with patch("subprocess.run") as mock_run:
        with patch.object(tmp_driver, "_dhclient_running", return_value=True):
            tmp_driver._stop_dhclient("eth0")

    # First call should be sudo kill
    calls = mock_run.call_args_list
    kill_calls = [c for c in calls if "kill" in c[0][0]]
    assert len(kill_calls) >= 1
    kill_cmd = kill_calls[0][0][0]
    assert kill_cmd[0] == "sudo"
    assert kill_cmd[1] == "kill"
    assert "12345" in kill_cmd


def test_stop_dhclient_releases_lease_with_sudo(tmp_driver: DnsmasqDriver) -> None:
    """_stop_dhclient should use sudo dhclient -r to release the lease."""
    pidfile = tmp_driver._pidfile_dir / "dhclient-eth0.pid"
    pidfile.write_text("12345")

    with patch("subprocess.run") as mock_run:
        with patch.object(tmp_driver, "_dhclient_running", return_value=True):
            tmp_driver._stop_dhclient("eth0")

    # dhclient -r call should use sudo
    calls = mock_run.call_args_list
    dhclient_calls = [c for c in calls if "dhclient" in c[0][0]]
    assert len(dhclient_calls) >= 1
    dhclient_cmd = dhclient_calls[0][0][0]
    assert dhclient_cmd[0] == "sudo"
    assert dhclient_cmd[1] == "dhclient"
    assert "-r" in dhclient_cmd
    assert "eth0" in dhclient_cmd
