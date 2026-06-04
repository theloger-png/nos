"""Unit tests for nos.utils.interface_alias."""
from __future__ import annotations

import copy
import json
import os
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from nos.utils.interface_alias import (
    generate_alias_map,
    load_alias_map,
    migrate_config,
    migrate_config_reverse,
    save_alias_map,
    to_alias,
    to_physical,
)


# ---------------------------------------------------------------------------
# generate_alias_map
# ---------------------------------------------------------------------------

class TestGenerateAliasMap:
    def test_basic(self):
        result = generate_alias_map(["ens33", "ens34"])
        assert result == {"ens33": "et0", "ens34": "et1"}

    def test_single(self):
        assert generate_alias_map(["eth0"]) == {"eth0": "et0"}

    def test_empty(self):
        assert generate_alias_map([]) == {}

    def test_preserves_order(self):
        result = generate_alias_map(["vmnic2", "vmnic0", "vmnic1"])
        assert list(result.values()) == ["et0", "et1", "et2"]


# ---------------------------------------------------------------------------
# to_alias
# ---------------------------------------------------------------------------

class TestToAlias:
    ALIAS_MAP = {"ens33": "et0", "ens34": "et1", "ens35": "et2"}

    def test_direct_match(self):
        assert to_alias("ens33", self.ALIAS_MAP) == "et0"
        assert to_alias("ens34", self.ALIAS_MAP) == "et1"

    def test_no_match_returned_unchanged(self):
        assert to_alias("lo", self.ALIAS_MAP) == "lo"
        assert to_alias("unknown0", self.ALIAS_MAP) == "unknown0"

    def test_subinterface(self):
        assert to_alias("ens34.101", self.ALIAS_MAP) == "et1.101"
        assert to_alias("ens33.0", self.ALIAS_MAP) == "et0.0"

    def test_subinterface_no_match(self):
        assert to_alias("eth99.10", self.ALIAS_MAP) == "eth99.10"

    def test_irb_unchanged(self):
        assert to_alias("irb.100", self.ALIAS_MAP) == "irb.100"

    def test_empty_map(self):
        assert to_alias("ens33", {}) == "ens33"


# ---------------------------------------------------------------------------
# to_physical
# ---------------------------------------------------------------------------

class TestToPhysical:
    ALIAS_MAP = {"ens33": "et0", "ens34": "et1"}

    def test_reverse_direct(self):
        assert to_physical("et0", self.ALIAS_MAP) == "ens33"
        assert to_physical("et1", self.ALIAS_MAP) == "ens34"

    def test_no_match_unchanged(self):
        assert to_physical("et99", self.ALIAS_MAP) == "et99"
        assert to_physical("lo", self.ALIAS_MAP) == "lo"

    def test_subinterface(self):
        assert to_physical("et1.101", self.ALIAS_MAP) == "ens34.101"
        assert to_physical("et0.0", self.ALIAS_MAP) == "ens33.0"

    def test_subinterface_no_match(self):
        assert to_physical("et99.10", self.ALIAS_MAP) == "et99.10"

    def test_irb_unchanged(self):
        assert to_physical("irb.100", self.ALIAS_MAP) == "irb.100"

    def test_roundtrip(self):
        alias_map = {"ens33": "et0", "ens34": "et1"}
        for phys in alias_map:
            alias = to_alias(phys, alias_map)
            assert to_physical(alias, alias_map) == phys

    def test_subinterface_roundtrip(self):
        alias_map = {"ens34": "et1"}
        assert to_physical(to_alias("ens34.200", alias_map), alias_map) == "ens34.200"


# ---------------------------------------------------------------------------
# save_alias_map / load_alias_map
# ---------------------------------------------------------------------------

class TestSaveLoad:
    def test_roundtrip(self, tmp_path):
        path = str(tmp_path / "map.json")
        alias_map = {"ens33": "et0", "ens34": "et1"}
        save_alias_map(alias_map, path)
        loaded = load_alias_map(path)
        assert loaded == alias_map

    def test_load_missing_returns_none(self, tmp_path):
        path = str(tmp_path / "nonexistent.json")
        assert load_alias_map(path) is None

    def test_save_creates_parents(self, tmp_path):
        path = str(tmp_path / "sub" / "dir" / "map.json")
        save_alias_map({"ens33": "et0"}, path)
        assert Path(path).exists()


# ---------------------------------------------------------------------------
# migrate_config
# ---------------------------------------------------------------------------

ALIAS_MAP = {"ens33": "et0", "ens34": "et1"}


class TestMigrateConfig:
    def _base_cfg(self):
        return {
            "system": {"host_name": "sw01", "interface_rename": True},
            "interfaces": {
                "ens33": {"description": "uplink", "family_inet": {"address": {"10.0.0.1/30": {}}}},
                "ens34": {"unit": {"0": {"family_ethernet_switching": {"interface_mode": "access"}}}},
                "irb": {"unit": {"100": {"family_inet": {"address": {"192.168.100.1/24": {}}}}}},
            },
            "vlans": {
                "vlan100": {"vlan_id": 100, "l3_interface": "irb.100"},
            },
            "protocols": {
                "isis": {
                    "interface": {
                        "ens33": {"point_to_point": True},
                        "lo0": {},
                    }
                }
            },
            "routing_instances": {
                "mgmt": {
                    "instance_type": "vrf",
                    "interface": ["ens33", "ens34"],
                }
            },
        }

    def test_interfaces_keys_translated(self):
        cfg = self._base_cfg()
        result = migrate_config(cfg, ALIAS_MAP)
        assert "et0" in result["interfaces"]
        assert "et1" in result["interfaces"]
        assert "ens33" not in result["interfaces"]
        assert "ens34" not in result["interfaces"]

    def test_irb_key_preserved(self):
        cfg = self._base_cfg()
        result = migrate_config(cfg, ALIAS_MAP)
        assert "irb" in result["interfaces"]

    def test_isis_interface_keys_translated(self):
        cfg = self._base_cfg()
        result = migrate_config(cfg, ALIAS_MAP)
        isis_ifaces = result["protocols"]["isis"]["interface"]
        assert "et0" in isis_ifaces
        assert "lo0" in isis_ifaces
        assert "ens33" not in isis_ifaces

    def test_routing_instance_interface_list_translated(self):
        cfg = self._base_cfg()
        result = migrate_config(cfg, ALIAS_MAP)
        ifaces = result["routing_instances"]["mgmt"]["interface"]
        assert "et0" in ifaces
        assert "et1" in ifaces
        assert "ens33" not in ifaces

    def test_does_not_mutate_original(self):
        cfg = self._base_cfg()
        original = copy.deepcopy(cfg)
        migrate_config(cfg, ALIAS_MAP)
        assert cfg == original

    def test_irb_l3_interface_unchanged(self):
        cfg = self._base_cfg()
        result = migrate_config(cfg, ALIAS_MAP)
        assert result["vlans"]["vlan100"]["l3_interface"] == "irb.100"

    def test_ip_addresses_unchanged(self):
        cfg = self._base_cfg()
        result = migrate_config(cfg, ALIAS_MAP)
        inet = result["interfaces"]["et0"]["family_inet"]["address"]
        assert "10.0.0.1/30" in inet

    def test_description_not_translated(self):
        cfg = {
            "interfaces": {
                "ens33": {"description": "ens33-uplink"},
            }
        }
        result = migrate_config(cfg, ALIAS_MAP)
        # The description is a substring containing "ens33" but not equal to it
        assert result["interfaces"]["et0"]["description"] == "ens33-uplink"

    def test_empty_config(self):
        result = migrate_config({}, ALIAS_MAP)
        assert result == {}

    def test_no_protocols_section(self):
        cfg = {"interfaces": {"ens33": {}}}
        result = migrate_config(cfg, ALIAS_MAP)
        assert "et0" in result["interfaces"]

    def test_subinterface_value_translated(self):
        # A string value like "ens33.0" should become "et0.0"
        cfg = {
            "some_section": {"ref_iface": "ens33.100"},
            "interfaces": {"ens33": {}},
        }
        result = migrate_config(cfg, ALIAS_MAP)
        assert result["some_section"]["ref_iface"] == "et0.100"


# ---------------------------------------------------------------------------
# migrate_config_reverse
# ---------------------------------------------------------------------------

class TestMigrateConfigReverse:
    def test_roundtrip(self):
        original = {
            "interfaces": {
                "ens33": {"description": "uplink"},
                "ens34": {},
                "irb": {},
            },
            "protocols": {
                "isis": {"interface": {"ens33": {}, "lo0": {}}}
            },
            "routing_instances": {
                "mgmt": {"interface": ["ens33", "ens34"]}
            },
        }
        migrated = migrate_config(original, ALIAS_MAP)
        restored = migrate_config_reverse(migrated, ALIAS_MAP)

        assert restored["interfaces"] == original["interfaces"]
        assert restored["protocols"]["isis"]["interface"] == \
               original["protocols"]["isis"]["interface"]
        assert sorted(restored["routing_instances"]["mgmt"]["interface"]) == \
               sorted(original["routing_instances"]["mgmt"]["interface"])

    def test_aliases_replaced(self):
        cfg = {
            "interfaces": {"et0": {}, "et1": {}},
            "protocols": {"isis": {"interface": {"et0": {}}}},
        }
        result = migrate_config_reverse(cfg, ALIAS_MAP)
        assert "ens33" in result["interfaces"]
        assert "ens34" in result["interfaces"]
        assert "ens33" in result["protocols"]["isis"]["interface"]

    def test_does_not_mutate_original(self):
        cfg = {"interfaces": {"et0": {}}}
        original = copy.deepcopy(cfg)
        migrate_config_reverse(cfg, ALIAS_MAP)
        assert cfg == original
