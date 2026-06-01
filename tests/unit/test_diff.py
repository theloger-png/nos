"""Unit tests for nos.config.diff."""
import pytest

from nos.config.diff import diff


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def has_line(output: str, sign: str, fragment: str) -> bool:
    for line in output.splitlines():
        if line.startswith(sign) and fragment in line:
            return True
    return False


def has_header(output: str, path: str) -> bool:
    return f"[edit {path}]" in output or (path == "" and "[edit]" in output)


# ---------------------------------------------------------------------------
# Identity
# ---------------------------------------------------------------------------

def test_identical_configs_produce_empty_diff():
    cfg = {"system": {"host_name": "r1"}}
    assert diff(cfg, cfg) == ""


def test_both_empty_produce_empty_diff():
    assert diff({}, {}) == ""


# ---------------------------------------------------------------------------
# Top-level additions and removals
# ---------------------------------------------------------------------------

def test_added_top_level_key():
    old = {}
    new = {"system": {"host_name": "r1"}}
    out = diff(old, new)
    assert has_header(out, "")
    assert has_line(out, "+", "system")


def test_removed_top_level_key():
    old = {"system": {"host_name": "r1"}}
    new = {}
    out = diff(old, new)
    assert has_header(out, "")
    assert has_line(out, "-", "system")


# ---------------------------------------------------------------------------
# Scalar value changes
# ---------------------------------------------------------------------------

def test_changed_scalar_shows_minus_and_plus():
    old = {"system": {"host_name": "old-router"}}
    new = {"system": {"host_name": "new-router"}}
    out = diff(old, new)
    assert has_header(out, "system")
    assert has_line(out, "-", "old-router")
    assert has_line(out, "+", "new-router")


def test_changed_integer_scalar():
    old = {"interfaces": {"eth0": {"mtu": 1500}}}
    new = {"interfaces": {"eth0": {"mtu": 9000}}}
    out = diff(old, new)
    assert has_header(out, "interfaces eth0")
    assert has_line(out, "-", "1500")
    assert has_line(out, "+", "9000")


# ---------------------------------------------------------------------------
# Nested additions / removals
# ---------------------------------------------------------------------------

def test_nested_key_added():
    old = {"interfaces": {"eth0": {"mtu": 1500}}}
    new = {"interfaces": {"eth0": {"mtu": 1500, "description": "uplink"}}}
    out = diff(old, new)
    assert has_header(out, "interfaces eth0")
    assert has_line(out, "+", "description")
    assert has_line(out, "+", "uplink")


def test_nested_key_removed():
    old = {"interfaces": {"eth0": {"mtu": 1500, "description": "uplink"}}}
    new = {"interfaces": {"eth0": {"mtu": 1500}}}
    out = diff(old, new)
    assert has_header(out, "interfaces eth0")
    assert has_line(out, "-", "description")


def test_new_interface_added():
    old = {"interfaces": {"eth0": {"mtu": 1500}}}
    new = {"interfaces": {"eth0": {"mtu": 1500}, "eth1": {"description": "new"}}}
    out = diff(old, new)
    assert has_header(out, "interfaces")
    assert has_line(out, "+", "eth1")


def test_interface_removed():
    old = {"interfaces": {"eth0": {}, "eth1": {"description": "old"}}}
    new = {"interfaces": {"eth0": {}}}
    out = diff(old, new)
    assert has_header(out, "interfaces")
    assert has_line(out, "-", "eth1")


# ---------------------------------------------------------------------------
# Boolean flags
# ---------------------------------------------------------------------------

def test_boolean_true_added():
    old = {"interfaces": {"eth0": {}}}
    new = {"interfaces": {"eth0": {"disable": True}}}
    out = diff(old, new)
    assert has_line(out, "+", "disable")


def test_boolean_true_removed():
    old = {"interfaces": {"eth0": {"disable": True}}}
    new = {"interfaces": {"eth0": {}}}
    out = diff(old, new)
    assert has_line(out, "-", "disable")


def test_boolean_false_not_rendered():
    old = {"interfaces": {"eth0": {"disable": False}}}
    new = {"interfaces": {"eth0": {"disable": True}}}
    out = diff(old, new)
    # False is invisible so this is effectively {} → {disable: True}
    assert has_line(out, "+", "disable")


# ---------------------------------------------------------------------------
# Deep nested diff
# ---------------------------------------------------------------------------

def test_deep_nested_change():
    old = {
        "protocols": {
            "bgp": {
                "group": {
                    "UPSTREAM": {"peer_as": 65001}
                }
            }
        }
    }
    new = {
        "protocols": {
            "bgp": {
                "group": {
                    "UPSTREAM": {"peer_as": 65002}
                }
            }
        }
    }
    out = diff(old, new)
    assert has_header(out, "protocols bgp group UPSTREAM")
    assert has_line(out, "-", "65001")
    assert has_line(out, "+", "65002")


# ---------------------------------------------------------------------------
# JunOS key format (snake_case → hyphen-case in output)
# ---------------------------------------------------------------------------

def test_keys_rendered_in_hyphen_case():
    old = {}
    new = {"routing_options": {"router_id": "1.1.1.1"}}
    out = diff(old, new)
    assert "routing-options" in out
    assert "router-id" in out
    assert "routing_options" not in out
    assert "router_id" not in out


# ---------------------------------------------------------------------------
# Multiple simultaneous changes
# ---------------------------------------------------------------------------

def test_multiple_changes_in_same_stanza():
    old = {"system": {"host_name": "r1", "domain_name": "old.local"}}
    new = {"system": {"host_name": "r2", "domain_name": "new.local"}}
    out = diff(old, new)
    assert has_header(out, "system")
    assert has_line(out, "-", "r1")
    assert has_line(out, "+", "r2")
    assert has_line(out, "-", "old.local")
    assert has_line(out, "+", "new.local")


def test_no_false_positives_for_unchanged_siblings():
    old = {"system": {"host_name": "r1", "domain_name": "lab.local"}}
    new = {"system": {"host_name": "r2", "domain_name": "lab.local"}}
    out = diff(old, new)
    # domain_name unchanged — should not appear in diff
    assert "lab.local" not in out


# ---------------------------------------------------------------------------
# List values
# ---------------------------------------------------------------------------

def test_list_value_added():
    old = {"system": {}}
    new = {"system": {"name_server": ["8.8.8.8", "8.8.4.4"]}}
    out = diff(old, new)
    assert has_line(out, "+", "8.8.8.8")
    assert has_line(out, "+", "8.8.4.4")


def test_list_value_removed():
    old = {"system": {"name_server": ["8.8.8.8"]}}
    new = {"system": {}}
    out = diff(old, new)
    assert has_line(out, "-", "8.8.8.8")


# ---------------------------------------------------------------------------
# Dict block rendering
# ---------------------------------------------------------------------------

def test_added_dict_block_has_braces():
    old = {}
    new = {"vlans": {"mgmt": {"vlan_id": 10}}}
    out = diff(old, new)
    assert "{" in out
    assert "}" in out
    lines = [l for l in out.splitlines() if l.strip().startswith("+")]
    assert any("vlans" in l for l in lines)


def test_removed_dict_block_has_braces():
    old = {"vlans": {"mgmt": {"vlan_id": 10}}}
    new = {}
    out = diff(old, new)
    lines = [l for l in out.splitlines() if l.strip().startswith("-")]
    assert any("{" in l or "vlans" in l for l in lines)
