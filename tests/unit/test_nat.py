"""Unit tests for NAT driver, show commands, serializer, and validator."""
from __future__ import annotations

from unittest.mock import MagicMock, call, patch

import pytest

from nos.config.schema import (
    NatConfig,
    NatDestinationConfig,
    NatDestinationRule,
    NatPool,
    NatSourceConfig,
    NatSourceRule,
    NatStaticConfig,
    NatStaticRule,
    NOSConfig,
    SecurityConfig,
)
from nos.config.serializer import from_set_commands, to_set_commands
from nos.config.validator import ConfigValidator
from nos.drivers.kernel.nat import NatDriver, _pool_range


# ---------------------------------------------------------------------------
# Pool range extraction
# ---------------------------------------------------------------------------

def test_pool_range_slash30():
    start, end = _pool_range("1.2.3.4/30")
    assert start == "1.2.3.5"
    assert end == "1.2.3.6"


def test_pool_range_slash29():
    start, end = _pool_range("10.0.0.0/29")
    assert start == "10.0.0.1"
    assert end == "10.0.0.6"


def test_pool_range_slash32():
    start, end = _pool_range("1.2.3.4/32")
    assert start == "1.2.3.4"
    assert end == "1.2.3.4"


def test_pool_range_slash31():
    start, end = _pool_range("192.168.1.0/31")
    assert start == "192.168.1.0"
    assert end == "192.168.1.1"


def test_pool_range_slash24():
    start, end = _pool_range("10.1.1.0/24")
    assert start == "10.1.1.1"
    assert end == "10.1.1.254"


# ---------------------------------------------------------------------------
# NatDriver.flush()
# ---------------------------------------------------------------------------

def test_nat_driver_flush_calls_nft():
    with patch("nos.drivers.kernel.nat.subprocess.run") as mock_run:
        mock_run.return_value = MagicMock(returncode=0)
        NatDriver().flush()
        mock_run.assert_called_once_with(
            ["nft", "delete", "table", "inet", "nos_nat"],
            capture_output=True,
        )


def test_nat_driver_flush_nft_not_found():
    with patch("nos.drivers.kernel.nat.subprocess.run", side_effect=FileNotFoundError):
        NatDriver().flush()  # Should not raise


# ---------------------------------------------------------------------------
# NatDriver.apply() — empty config
# ---------------------------------------------------------------------------

def test_nat_driver_apply_empty_calls_flush():
    nat = NatConfig()
    with patch.object(NatDriver, "flush") as mock_flush:
        NatDriver().apply(nat)
        mock_flush.assert_called_once()


# ---------------------------------------------------------------------------
# NatDriver.apply() — static SNAT rule
# ---------------------------------------------------------------------------

def test_nat_driver_apply_static_snat():
    nat = NatConfig(
        static=NatStaticConfig(rule={
            "R1": NatStaticRule(source="192.168.1.10/32", translated="1.2.3.4"),
        })
    )
    with patch("nos.drivers.kernel.nat.subprocess.run") as mock_run:
        mock_run.return_value = MagicMock(returncode=0)
        NatDriver().apply(nat)

    calls = mock_run.call_args_list
    # First call is flush, second is nft -f -
    assert calls[0] == call(
        ["nft", "delete", "table", "inet", "nos_nat"], capture_output=True
    )
    nft_call = calls[1]
    assert nft_call[0][0] == ["nft", "-f", "-"]
    ruleset = nft_call[1]["input"]
    assert "snat to 1.2.3.4" in ruleset
    assert "ip saddr 192.168.1.10/32" in ruleset
    assert 'iifname != "lo"' in ruleset
    assert "chain postrouting" in ruleset


# ---------------------------------------------------------------------------
# NatDriver.apply() — pool SNAT rule
# ---------------------------------------------------------------------------

def test_nat_driver_apply_pool_snat():
    nat = NatConfig(
        pool={"POOL1": NatPool(address="1.2.3.4/30")},
        source=NatSourceConfig(rule={
            "R1": NatSourceRule(
                match_source="192.168.1.0/24",
                then_pool="POOL1",
                interface="et0",
            ),
        }),
    )
    with patch("nos.drivers.kernel.nat.subprocess.run") as mock_run:
        mock_run.return_value = MagicMock(returncode=0)
        NatDriver().apply(nat, alias_to_kernel_fn=lambda n: "ens33" if n == "et0" else n)

    ruleset = mock_run.call_args_list[1][1]["input"]
    assert 'oifname "ens33"' in ruleset
    assert "ip saddr 192.168.1.0/24" in ruleset
    assert "snat to 1.2.3.5-1.2.3.6" in ruleset


def test_nat_driver_apply_pool_snat_slash32():
    nat = NatConfig(
        pool={"P": NatPool(address="203.0.113.5/32")},
        source=NatSourceConfig(rule={
            "R1": NatSourceRule(
                match_source="10.0.0.0/8",
                then_pool="P",
                interface="eth0",
            ),
        }),
    )
    with patch("nos.drivers.kernel.nat.subprocess.run") as mock_run:
        mock_run.return_value = MagicMock(returncode=0)
        NatDriver().apply(nat)

    ruleset = mock_run.call_args_list[1][1]["input"]
    assert "snat to 203.0.113.5" in ruleset
    assert "203.0.113.5-203.0.113.5" not in ruleset


# ---------------------------------------------------------------------------
# NatDriver.apply() — DNAT without port
# ---------------------------------------------------------------------------

def test_nat_driver_apply_dnat_no_port():
    nat = NatConfig(
        destination=NatDestinationConfig(rule={
            "R1": NatDestinationRule(
                match_destination="1.2.3.4",
                then_destination="192.168.1.10",
            ),
        })
    )
    with patch("nos.drivers.kernel.nat.subprocess.run") as mock_run:
        mock_run.return_value = MagicMock(returncode=0)
        NatDriver().apply(nat)

    ruleset = mock_run.call_args_list[1][1]["input"]
    assert "ip daddr 1.2.3.4 dnat to 192.168.1.10" in ruleset
    assert "chain prerouting" in ruleset


# ---------------------------------------------------------------------------
# NatDriver.apply() — DNAT with port
# ---------------------------------------------------------------------------

def test_nat_driver_apply_dnat_with_port():
    nat = NatConfig(
        destination=NatDestinationConfig(rule={
            "R1": NatDestinationRule(
                match_destination="1.2.3.4",
                match_destination_port=80,
                then_destination="192.168.1.10",
                then_destination_port=8080,
            ),
        })
    )
    with patch("nos.drivers.kernel.nat.subprocess.run") as mock_run:
        mock_run.return_value = MagicMock(returncode=0)
        NatDriver().apply(nat)

    ruleset = mock_run.call_args_list[1][1]["input"]
    assert "tcp dport 80 ip daddr 1.2.3.4 dnat to 192.168.1.10:8080" in ruleset
    assert "udp dport 80 ip daddr 1.2.3.4 dnat to 192.168.1.10:8080" in ruleset


# ---------------------------------------------------------------------------
# NatDriver.apply() — nft not found
# ---------------------------------------------------------------------------

def test_nat_driver_apply_nft_not_found():
    nat = NatConfig(
        static=NatStaticConfig(rule={
            "R1": NatStaticRule(source="10.0.0.1/32", translated="1.2.3.4"),
        })
    )
    with patch("nos.drivers.kernel.nat.subprocess.run", side_effect=[
        MagicMock(returncode=0),   # flush
        FileNotFoundError,          # nft -f -
    ]):
        NatDriver().apply(nat)   # Should not raise


# ---------------------------------------------------------------------------
# Show commands formatting
# ---------------------------------------------------------------------------

def _running_with_nat() -> dict:
    return {
        "security": {
            "nat": {
                "static": {
                    "rule": {
                        "R1": {"source": "192.168.1.10/32", "translated": "1.2.3.4"},
                    }
                },
                "pool": {
                    "POOL1": {"address": "1.2.3.4/30"},
                },
                "source": {
                    "rule": {
                        "R1": {
                            "match_source": "192.168.1.0/24",
                            "then_pool": "POOL1",
                            "interface": "et0",
                        }
                    }
                },
                "destination": {
                    "rule": {
                        "R1": {
                            "match_destination": "1.2.3.4",
                            "match_destination_port": 80,
                            "then_destination": "192.168.1.10",
                            "then_destination_port": 8080,
                        }
                    }
                },
            }
        }
    }


def test_show_nat_static():
    from nos.cli.commands.show.nat import show_nat_static
    out = show_nat_static(_running_with_nat())
    assert "R1" in out
    assert "192.168.1.10/32" in out
    assert "1.2.3.4" in out


def test_show_nat_static_empty():
    from nos.cli.commands.show.nat import show_nat_static
    out = show_nat_static({})
    assert "No static NAT rules" in out


def test_show_nat_source():
    from nos.cli.commands.show.nat import show_nat_source
    out = show_nat_source(_running_with_nat())
    assert "R1" in out
    assert "192.168.1.0/24" in out
    assert "POOL1" in out
    assert "et0" in out


def test_show_nat_pool():
    from nos.cli.commands.show.nat import show_nat_pool
    out = show_nat_pool(_running_with_nat())
    assert "POOL1" in out
    assert "1.2.3.4/30" in out


def test_show_nat_pool_empty():
    from nos.cli.commands.show.nat import show_nat_pool
    out = show_nat_pool({})
    assert "No NAT pools" in out


def test_show_nat_destination():
    from nos.cli.commands.show.nat import show_nat_destination
    out = show_nat_destination(_running_with_nat())
    assert "R1" in out
    assert "1.2.3.4" in out
    assert "80" in out
    assert "192.168.1.10" in out
    assert "8080" in out


def test_show_nat_translations_nft_not_found():
    from nos.cli.commands.show.nat import show_nat_translations
    with patch("nos.cli.commands.show.nat.subprocess.run", side_effect=FileNotFoundError):
        out = show_nat_translations()
    assert "not found" in out


def test_show_nat_translations_table_missing():
    from nos.cli.commands.show.nat import show_nat_translations
    mock_result = MagicMock(returncode=1, stderr="Error: No such file or directory")
    with patch("nos.cli.commands.show.nat.subprocess.run", return_value=mock_result):
        out = show_nat_translations()
    assert "not active" in out


# ---------------------------------------------------------------------------
# Serializer round-trip
# ---------------------------------------------------------------------------

def test_serializer_static_nat_roundtrip():
    config = {
        "security": {
            "nat": {
                "static": {
                    "rule": {
                        "R1": {"source": "192.168.1.10/32", "translated": "1.2.3.4"}
                    }
                }
            }
        }
    }
    cmds = to_set_commands(config)
    assert any("security" in c and "static" in c and "rule" in c and "R1" in c for c in cmds)
    assert any("source" in c and "192.168.1.10/32" in c for c in cmds)
    assert any("translated" in c and "1.2.3.4" in c for c in cmds)
    restored = from_set_commands(cmds)
    assert restored["security"]["nat"]["static"]["rule"]["R1"]["source"] == "192.168.1.10/32"
    assert restored["security"]["nat"]["static"]["rule"]["R1"]["translated"] == "1.2.3.4"


def test_serializer_pool_roundtrip():
    config = {"security": {"nat": {"pool": {"POOL1": {"address": "1.2.3.4/30"}}}}}
    cmds = to_set_commands(config)
    assert any("pool" in c and "POOL1" in c and "address" in c for c in cmds)
    restored = from_set_commands(cmds)
    assert restored["security"]["nat"]["pool"]["POOL1"]["address"] == "1.2.3.4/30"


def test_serializer_source_rule_roundtrip():
    config = {
        "security": {
            "nat": {
                "source": {
                    "rule": {
                        "R1": {
                            "match_source": "192.168.1.0/24",
                            "then_pool": "POOL1",
                            "interface": "et0",
                        }
                    }
                }
            }
        }
    }
    cmds = to_set_commands(config)
    assert any("match" in c and "source" in c for c in cmds)
    assert any("then" in c and "pool" in c for c in cmds)
    restored = from_set_commands(cmds)
    rule = restored["security"]["nat"]["source"]["rule"]["R1"]
    assert rule["match_source"] == "192.168.1.0/24"
    assert rule["then_pool"] == "POOL1"
    assert rule["interface"] == "et0"


def test_serializer_destination_rule_roundtrip():
    config = {
        "security": {
            "nat": {
                "destination": {
                    "rule": {
                        "R1": {
                            "match_destination": "1.2.3.4",
                            "match_destination_port": 80,
                            "then_destination": "192.168.1.10",
                            "then_destination_port": 8080,
                        }
                    }
                }
            }
        }
    }
    cmds = to_set_commands(config)
    assert any("match" in c and "destination" in c and "1.2.3.4" in c for c in cmds)
    assert any("match" in c and "destination-port" in c and "80" in c for c in cmds)
    restored = from_set_commands(cmds)
    rule = restored["security"]["nat"]["destination"]["rule"]["R1"]
    assert rule["match_destination"] == "1.2.3.4"
    assert rule["match_destination_port"] == 80
    assert rule["then_destination"] == "192.168.1.10"
    assert rule["then_destination_port"] == 8080


# ---------------------------------------------------------------------------
# Validator
# ---------------------------------------------------------------------------

def _base_interfaces() -> dict:
    return {
        "interfaces": {
            "et0": {
                "unit": {"0": {"family_inet": {"address": {"10.0.0.1/30": {}}}}}
            }
        }
    }


def test_validator_valid_nat():
    config = {
        **_base_interfaces(),
        "security": {
            "nat": {
                "pool": {"POOL1": {"address": "1.2.3.4/30"}},
                "source": {
                    "rule": {
                        "R1": {
                            "match_source": "192.168.1.0/24",
                            "then_pool": "POOL1",
                            "interface": "et0",
                        }
                    }
                },
            }
        },
    }
    result = ConfigValidator().validate(config)
    assert result.is_valid, [str(e) for e in result.errors]


def test_validator_invalid_pool_reference():
    config = {
        **_base_interfaces(),
        "security": {
            "nat": {
                "source": {
                    "rule": {
                        "R1": {
                            "match_source": "192.168.1.0/24",
                            "then_pool": "NONEXISTENT",
                            "interface": "et0",
                        }
                    }
                },
            }
        },
    }
    result = ConfigValidator().validate(config)
    assert not result.is_valid
    assert any("NONEXISTENT" in str(e) for e in result.errors)


def test_validator_invalid_interface_reference():
    config = {
        **_base_interfaces(),
        "security": {
            "nat": {
                "pool": {"P": {"address": "1.2.3.4/30"}},
                "source": {
                    "rule": {
                        "R1": {
                            "match_source": "10.0.0.0/8",
                            "then_pool": "P",
                            "interface": "nonexistent0",
                        }
                    }
                },
            }
        },
    }
    result = ConfigValidator().validate(config)
    assert not result.is_valid
    assert any("nonexistent0" in str(e) for e in result.errors)


def test_validator_invalid_static_rule_source():
    config = {
        "security": {
            "nat": {
                "static": {
                    "rule": {"R1": {"source": "not-an-ip", "translated": "1.2.3.4"}}
                }
            }
        }
    }
    result = ConfigValidator().validate(config)
    assert not result.is_valid


def test_validator_invalid_static_rule_translated():
    config = {
        "security": {
            "nat": {
                "static": {
                    "rule": {"R1": {"source": "192.168.1.0/24", "translated": "bad-ip"}}
                }
            }
        }
    }
    result = ConfigValidator().validate(config)
    assert not result.is_valid


def test_validator_destination_port_out_of_range():
    config = {
        "security": {
            "nat": {
                "destination": {
                    "rule": {
                        "R1": {
                            "match_destination": "1.2.3.4",
                            "match_destination_port": 99999,
                            "then_destination": "10.0.0.1",
                        }
                    }
                }
            }
        }
    }
    result = ConfigValidator().validate(config)
    assert not result.is_valid


def test_validator_empty_security_section_valid():
    result = ConfigValidator().validate({})
    assert result.is_valid
