"""Tests for dotted-notation interface expansion in expand_config_tokens.

Rules:
- 'interfaces ens34.0 ...' expands to 'interfaces ens34 unit 0 ...'
- 'protocols isis interface ens34.0' is kept as-is
- 'routing-instances VRF interface ens34.0' is kept as-is
"""
from __future__ import annotations

import pytest

from nos.cli.completer import expand_config_tokens
from nos.cli.modes.configure import ConfigureMode
from nos.config.commit import CommitEngine
from nos.config.store import ConfigStore
from nos.config.validator import ConfigValidator


# ============================================================================
# expand_config_tokens — unit tests
# ============================================================================

class TestExpandDottedUnit:
    """Direct tests of the token expansion logic."""

    def _expand(self, tokens: list[str]) -> list[str]:
        result, err = expand_config_tokens(tokens)
        assert err is None, f"unexpected error: {err}"
        assert result is not None
        return result

    # --- expansion cases (under interfaces) ---

    def test_set_inet_address(self):
        tokens = ["interfaces", "ens34.0", "family", "inet", "address", "10.0.0.1/24"]
        assert self._expand(tokens) == [
            "interfaces", "ens34", "unit", "0", "family", "inet", "address", "10.0.0.1/24"
        ]

    def test_delete_dotted(self):
        tokens = ["interfaces", "ens34.0"]
        assert self._expand(tokens) == ["interfaces", "ens34", "unit", "0"]

    def test_edit_dotted(self):
        tokens = ["interfaces", "ens34.0"]
        assert self._expand(tokens) == ["interfaces", "ens34", "unit", "0"]

    def test_irb_unit_100(self):
        tokens = ["interfaces", "irb.100", "family", "inet", "address", "192.168.100.1/24"]
        assert self._expand(tokens) == [
            "interfaces", "irb", "unit", "100", "family", "inet", "address", "192.168.100.1/24"
        ]

    def test_unit_zero_ethernet_switching(self):
        tokens = ["interfaces", "eth1.0", "family", "ethernet-switching", "interface-mode", "trunk"]
        assert self._expand(tokens) == [
            "interfaces", "eth1", "unit", "0",
            "family", "ethernet-switching", "interface-mode", "trunk"
        ]

    def test_multi_digit_unit(self):
        tokens = ["interfaces", "eth0.100", "family", "inet", "address", "10.0.0.1/24"]
        assert self._expand(tokens) == [
            "interfaces", "eth0", "unit", "100", "family", "inet", "address", "10.0.0.1/24"
        ]

    def test_no_trailing_tokens(self):
        """Bare dotted interface with no further path."""
        tokens = ["interfaces", "ens34.0"]
        assert self._expand(tokens) == ["interfaces", "ens34", "unit", "0"]

    # --- non-expansion cases ---

    def test_protocols_isis_interface_kept(self):
        """ens34.0 after protocols isis interface must NOT be expanded."""
        tokens = ["protocols", "isis", "interface", "ens34.0", "point-to-point"]
        result = self._expand(tokens)
        assert result == ["protocols", "isis", "interface", "ens34.0", "point-to-point"]

    def test_routing_instances_interface_kept(self):
        """ens34.0 after routing-instances <name> interface must NOT be expanded."""
        tokens = ["routing-instances", "VRF", "interface", "ens34.0"]
        result = self._expand(tokens)
        assert result == ["routing-instances", "VRF", "interface", "ens34.0"]

    def test_non_dotted_interface_unchanged(self):
        """Plain interface names are never altered."""
        tokens = ["interfaces", "ens34", "description", "uplink"]
        assert self._expand(tokens) == ["interfaces", "ens34", "description", "uplink"]

    def test_dotted_with_alphabetic_suffix_not_expanded(self):
        """ens34.abc does not match the <name>.<digits> pattern."""
        tokens = ["interfaces", "ens34.abc", "description", "x"]
        # The token is passed through as a dynamic interface name unchanged
        result = self._expand(tokens)
        assert result[0] == "interfaces"
        assert result[1] == "ens34.abc"

    def test_double_dot_not_expanded(self):
        """ens34.0.1 has two dots and must not be expanded."""
        tokens = ["interfaces", "ens34.0.1"]
        result = self._expand(tokens)
        assert result[1] == "ens34.0.1"


# ============================================================================
# ConfigureMode integration — set / delete / edit commands
# ============================================================================

@pytest.fixture
def store(tmp_path):
    return ConfigStore(base_dir=tmp_path)


@pytest.fixture
def engine(store):
    return CommitEngine(store, validator=ConfigValidator())


@pytest.fixture
def mode(store, engine):
    return ConfigureMode(store, engine)


class TestConfigureModeExpansion:
    """End-to-end tests through ConfigureMode.execute()."""

    def test_set_inet_address_via_dotted(self, mode, store):
        err = mode.execute("set interfaces ens34.0 family inet address 10.0.0.1/24")
        assert err == ""
        cfg = store.get_candidate()
        unit = cfg["interfaces"]["ens34"]["unit"]["0"]
        assert "10.0.0.1/24" in unit["family_inet"]["address"]

    def test_set_unit_description_via_dotted(self, mode, store):
        err = mode.execute("set interfaces eth0.0 family ethernet-switching interface-mode access")
        assert err == ""
        cfg = store.get_candidate()
        unit = cfg["interfaces"]["eth0"]["unit"]["0"]
        assert unit["family_ethernet_switching"]["interface_mode"] == "access"

    def test_delete_via_dotted(self, mode, store):
        mode.execute("set interfaces ens34.0 family inet address 10.0.0.1/24")
        err = mode.execute("delete interfaces ens34.0")
        assert err == ""
        cfg = store.get_candidate()
        assert "0" not in cfg.get("interfaces", {}).get("ens34", {}).get("unit", {})

    def test_edit_via_dotted(self, mode):
        err = mode.execute("edit interfaces ens34.0")
        assert err == ""
        assert mode.edit_path == ["interfaces", "ens34", "unit", "0"]

    def test_edit_then_set_family_relative(self, mode, store):
        mode.execute("edit interfaces ens34.0")
        err = mode.execute("set family inet address 10.1.1.1/30")
        assert err == ""
        cfg = store.get_candidate()
        unit = cfg["interfaces"]["ens34"]["unit"]["0"]
        assert "10.1.1.1/30" in unit["family_inet"]["address"]

    def test_protocols_isis_not_expanded(self, mode, store):
        err = mode.execute("set protocols isis interface ens34.0 point-to-point")
        assert err == ""
        cfg = store.get_candidate()
        iface_cfg = cfg["protocols"]["isis"]["interface"]
        assert "ens34.0" in iface_cfg
        assert "ens34" not in iface_cfg

    def test_routing_instances_not_expanded(self, mode, store):
        err = mode.execute("set routing-instances VRF interface ens34.0")
        assert err == ""
        cfg = store.get_candidate()
        iface_cfg = cfg["routing_instances"]["VRF"]["interface"]
        assert "ens34.0" in iface_cfg
