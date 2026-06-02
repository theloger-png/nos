"""Unit tests for nos.cli.completer."""
from __future__ import annotations

import pytest
from prompt_toolkit.completion import CompleteEvent
from prompt_toolkit.document import Document

from nos.cli.completer import (
    CONFIG_TREE,
    ConfigNode,
    NOSCompleter,
    build_config_tree,
    complete_config_tokens,
    navigate_tree,
)
from nos.cli.parser import CLIMode


# ============================================================================
# Helpers
# ============================================================================

def complete(text: str, mode: CLIMode, edit_path: list[str] | None = None) -> list[str]:
    """Return list of completion strings for *text*."""
    c = NOSCompleter(mode=mode, edit_path=edit_path or [], store=None)
    doc = Document(text, len(text))
    return [comp.text for comp in c.get_completions(doc, CompleteEvent())]


def complete_meta(text: str, mode: CLIMode) -> dict[str, str]:
    """Return {keyword: meta} for completions of *text*."""
    c = NOSCompleter(mode=mode, edit_path=[], store=None)
    doc = Document(text, len(text))
    return {
        comp.text: str(comp.display_meta)
        for comp in c.get_completions(doc, CompleteEvent())
    }


# ============================================================================
# Config tree structure
# ============================================================================

class TestConfigTree:
    def test_root_has_system(self):
        assert "system" in CONFIG_TREE.children

    def test_root_has_interfaces(self):
        assert "interfaces" in CONFIG_TREE.children

    def test_root_has_vlans(self):
        assert "vlans" in CONFIG_TREE.children

    def test_root_has_routing_options(self):
        assert "routing-options" in CONFIG_TREE.children

    def test_root_has_protocols(self):
        assert "protocols" in CONFIG_TREE.children

    def test_root_has_policy_options(self):
        assert "policy-options" in CONFIG_TREE.children

    def test_root_has_routing_instances(self):
        assert "routing-instances" in CONFIG_TREE.children

    def test_interfaces_is_dynamic(self):
        node = CONFIG_TREE.children["interfaces"]
        assert node.dynamic_child is not None

    def test_interface_inner_has_description(self):
        iface = CONFIG_TREE.children["interfaces"].dynamic_child
        assert iface is not None
        assert "description" in iface.children

    def test_description_is_value_node(self):
        iface = CONFIG_TREE.children["interfaces"].dynamic_child
        desc = iface.children["description"]
        assert desc.is_value

    def test_disable_is_presence_node(self):
        iface = CONFIG_TREE.children["interfaces"].dynamic_child
        assert iface.children["disable"].is_presence

    def test_speed_has_enum_choices(self):
        iface = CONFIG_TREE.children["interfaces"].dynamic_child
        speed = iface.children["speed"]
        assert "1g" in speed.enum_choices
        assert "100g" in speed.enum_choices

    def test_bgp_group_is_dynamic(self):
        bgp = CONFIG_TREE.children["protocols"].children["bgp"]
        assert bgp.children["group"].dynamic_child is not None

    def test_system_host_name_is_value(self):
        sys_node = CONFIG_TREE.children["system"]
        assert sys_node.children["host-name"].is_value

    def test_vlan_inner_has_vlan_id(self):
        vlans = CONFIG_TREE.children["vlans"]
        vlan_inner = vlans.dynamic_child
        assert vlan_inner is not None
        assert "vlan-id" in vlan_inner.children

    def test_unit_inner_has_vlan_id(self):
        ifaces = CONFIG_TREE.children["interfaces"]
        iface_inner = ifaces.dynamic_child
        unit_dyn = iface_inner.children["unit"]
        unit_inner = unit_dyn.dynamic_child
        assert unit_inner is not None
        node = unit_inner.children["vlan-id"]
        assert node.is_value
        assert node.value_hint == "<1-4094>"


# ============================================================================
# navigate_tree
# ============================================================================

class TestNavigateTree:
    def test_navigate_to_system(self):
        node = navigate_tree(CONFIG_TREE, ["system"])
        assert node is not None
        assert "host-name" in node.children

    def test_navigate_to_interfaces_dynamic(self):
        # "eth0" is a dynamic name under interfaces
        node = navigate_tree(CONFIG_TREE, ["interfaces", "eth0"])
        assert node is not None
        assert "description" in node.children

    def test_navigate_to_bgp_group_neighbor(self):
        node = navigate_tree(CONFIG_TREE,
                             ["protocols", "bgp", "group", "IBGP", "neighbor"])
        assert node is not None
        assert node.dynamic_child is not None

    def test_navigate_unknown_path_returns_none(self):
        node = navigate_tree(CONFIG_TREE, ["nonexistent"])
        assert node is None

    def test_navigate_empty_path_returns_root(self):
        node = navigate_tree(CONFIG_TREE, [])
        assert node is CONFIG_TREE

    def test_navigate_deep_static(self):
        node = navigate_tree(CONFIG_TREE,
                             ["routing-options", "static", "route"])
        assert node is not None
        assert node.dynamic_child is not None


# ============================================================================
# complete_config_tokens
# ============================================================================

class TestCompleteConfigTokens:
    def test_empty_tokens_at_root(self):
        results = complete_config_tokens([], True, [])
        kws = [c.text for c in results]
        assert "system" in kws
        assert "interfaces" in kws
        assert "vlans" in kws

    def test_system_children(self):
        results = complete_config_tokens(["system"], True, [])
        kws = [c.text for c in results]
        assert "host-name" in kws
        assert "ntp" in kws

    def test_prefix_filter(self):
        results = complete_config_tokens([], False, [], store=None)
        # All root keywords start with empty prefix → all returned
        assert len(results) > 0

    def test_no_completions_past_value_node(self):
        # After "host-name" (is_value), the next token is the value
        results = complete_config_tokens(["system", "host-name", "nos01"], True, [])
        assert results == []

    def test_routing_options_children(self):
        results = complete_config_tokens(["routing-options"], True, [])
        kws = [c.text for c in results]
        assert "static" in kws
        assert "router-id" in kws
        assert "autonomous-system" in kws

    def test_speed_enum_completions(self):
        results = complete_config_tokens(
            ["interfaces", "eth0", "speed"], True, []
        )
        kws = [c.text for c in results]
        assert "auto" in kws
        assert "1g" in kws
        assert "100g" in kws

    def test_completing_partial_keyword(self):
        # "syst" as partial prefix
        results = complete_config_tokens(["syst"], False, [])
        kws = [c.text for c in results]
        assert "system" in kws
        # Should NOT contain non-matching keywords
        assert "interfaces" not in kws

    def test_dynamic_child_hint_appears(self):
        # Under "interfaces", should show dynamic hint <interface-name>
        results = complete_config_tokens([], True, ["interfaces"])
        kws = [c.text for c in results]
        assert "<interface-name>" in kws

    def test_edit_path_shifts_start(self):
        # edit_path=["interfaces", "eth0"] → completions are interface keywords
        results = complete_config_tokens([], True, ["interfaces", "eth0"])
        kws = [c.text for c in results]
        assert "description" in kws
        assert "family" in kws


# ============================================================================
# NOSCompleter — operational mode
# ============================================================================

class TestOperationalCompleter:
    def test_empty_input_returns_all_commands(self):
        kws = complete("", CLIMode.OPERATIONAL)
        assert "show" in kws
        assert "ping" in kws
        assert "traceroute" in kws
        assert "configure" in kws

    def test_partial_command_filters(self):
        kws = complete("sh", CLIMode.OPERATIONAL)
        assert "show" in kws
        assert "ping" not in kws

    def test_show_space_returns_subcommands(self):
        kws = complete("show ", CLIMode.OPERATIONAL)
        assert "interfaces" in kws
        assert "route" in kws
        assert "bgp" in kws

    def test_show_partial_subcommand(self):
        kws = complete("show int", CLIMode.OPERATIONAL)
        assert "interfaces" in kws
        assert "route" not in kws

    def test_ping_shows_host_hint(self):
        kws = complete("ping ", CLIMode.OPERATIONAL)
        assert any("<host>" in k for k in kws)

    def test_traceroute_shows_host_hint(self):
        kws = complete("traceroute ", CLIMode.OPERATIONAL)
        assert any("<host>" in k for k in kws)

    # Abbreviated command dispatch
    def test_abbreviated_command_dispatches_subcommands(self):
        # 'sho ' should dispatch into show and return its sub-commands
        kws = complete("sho ", CLIMode.OPERATIONAL)
        assert "interfaces" in kws
        assert "route" in kws

    def test_abbreviated_command_with_abbreviated_subcommand(self):
        # 'sho int' should complete 'interfaces' at the first-level filter
        kws = complete("sho int", CLIMode.OPERATIONAL)
        assert "interfaces" in kws

    def test_abbreviated_show_interfaces_subcmds(self):
        # 'show int ' should show terse/description sub-commands
        kws = complete("show int ", CLIMode.OPERATIONAL)
        assert "terse" in kws
        assert "description" in kws

    def test_abbreviated_show_abbreviated_interfaces_subcmds(self):
        # 'sho int ' should also show terse/description sub-commands
        kws = complete("sho int ", CLIMode.OPERATIONAL)
        assert "terse" in kws
        assert "description" in kws

    def test_abbreviated_show_interfaces_partial_subcmd(self):
        # 'show int ter' should complete to 'terse'
        kws = complete("show int ter", CLIMode.OPERATIONAL)
        assert "terse" in kws
        assert "description" not in kws

    def test_ambiguous_command_prefix_yields_nothing(self):
        # 'e' matches both 'exit' and (in configure) multiple — in operational: exit only
        # 't' matches traceroute only → should give subcommands
        kws = complete("tr ", CLIMode.OPERATIONAL)
        assert any("<host>" in k for k in kws)

    def test_abbreviated_ping_dispatches(self):
        kws = complete("pi ", CLIMode.OPERATIONAL)
        assert any("<host>" in k for k in kws)


# ============================================================================
# NOSCompleter — configure mode (command keywords)
# ============================================================================

class TestConfigureCommandKeywords:
    def test_empty_input_returns_all_config_commands(self):
        kws = complete("", CLIMode.CONFIGURE)
        for cmd in ("set", "delete", "edit", "up", "top", "show",
                    "commit", "rollback", "discard", "run", "exit"):
            assert cmd in kws

    def test_partial_command(self):
        kws = complete("co", CLIMode.CONFIGURE)
        assert "commit" in kws
        assert "configure" not in kws  # not a configure-mode command

    def test_commit_subcommands(self):
        kws = complete("commit ", CLIMode.CONFIGURE)
        assert "confirmed" in kws
        assert "check" in kws

    def test_commit_confirmed_shows_minutes_hint(self):
        kws = complete("commit confirmed ", CLIMode.CONFIGURE)
        assert any("<minutes>" in k for k in kws)

    def test_rollback_shows_range_hint(self):
        kws = complete("rollback ", CLIMode.CONFIGURE)
        assert any("<0-49>" in k for k in kws)


# ============================================================================
# NOSCompleter — configure mode (set path completions)
# ============================================================================

class TestConfigureSetCompletions:
    def test_set_space_shows_root_hierarchy(self):
        kws = complete("set ", CLIMode.CONFIGURE)
        assert "system" in kws
        assert "interfaces" in kws
        assert "routing-options" in kws

    def test_set_system_space(self):
        kws = complete("set system ", CLIMode.CONFIGURE)
        assert "host-name" in kws
        assert "ntp" in kws

    def test_set_interfaces_space_shows_dynamic_hint(self):
        kws = complete("set interfaces ", CLIMode.CONFIGURE)
        assert "<interface-name>" in kws

    def test_set_interface_speed_shows_enums(self):
        kws = complete("set interfaces eth0 speed ", CLIMode.CONFIGURE)
        assert "auto" in kws
        assert "1g" in kws
        assert "10g" in kws

    def test_set_interface_duplex_shows_enums(self):
        kws = complete("set interfaces eth0 duplex ", CLIMode.CONFIGURE)
        assert "auto" in kws
        assert "full" in kws
        assert "half" in kws

    def test_set_protocols_shows_protocols(self):
        kws = complete("set protocols ", CLIMode.CONFIGURE)
        assert "isis" in kws
        assert "bgp" in kws

    def test_set_bgp_group_shows_hint(self):
        kws = complete("set protocols bgp group ", CLIMode.CONFIGURE)
        assert "<group-name>" in kws

    def test_set_bgp_group_type_enum(self):
        kws = complete("set protocols bgp group IBGP type ", CLIMode.CONFIGURE)
        assert "internal" in kws
        assert "external" in kws

    def test_edit_space_shows_root(self):
        kws = complete("edit ", CLIMode.CONFIGURE)
        assert "interfaces" in kws
        assert "system" in kws

    def test_delete_space_shows_root(self):
        kws = complete("delete ", CLIMode.CONFIGURE)
        assert "interfaces" in kws

    def test_set_with_edit_path_shifts_root(self):
        # When inside (interfaces eth0), 'set ' should complete interface keywords
        kws = complete("set ", CLIMode.CONFIGURE,
                        edit_path=["interfaces", "eth0"])
        assert "description" in kws
        assert "family" in kws
        # Root-level keywords should NOT appear
        assert "system" not in kws

    def test_set_routing_instances_type_enum(self):
        kws = complete(
            "set routing-instances VRF1 instance-type ", CLIMode.CONFIGURE
        )
        assert "vrf" in kws
        assert "virtual-router" in kws

    def test_show_configure_pipe_compare(self):
        kws = complete("show | ", CLIMode.CONFIGURE)
        assert "compare" in kws

    def test_run_shows_operational_commands(self):
        kws = complete("run ", CLIMode.CONFIGURE)
        assert "show" in kws
        assert "ping" in kws


# ============================================================================
# NOSCompleter — abbreviated command and token prefix matching
# ============================================================================

class TestAbbreviatedPrefixCompletion:
    """Verify resolve_prefix is used for commands and config walk tokens."""

    # Configure mode: abbreviated top-level command dispatch
    def test_abbreviated_set_dispatches(self):
        kws = complete("se ", CLIMode.CONFIGURE)
        assert "system" in kws
        assert "interfaces" in kws

    def test_abbreviated_set_with_abbreviated_section(self):
        # 'set int ' should show interface-level completions (abbreviated walk token)
        kws = complete("set int ", CLIMode.CONFIGURE)
        assert "<interface-name>" in kws

    def test_abbreviated_set_deep_walk(self):
        # Abbreviated walk: 'int' → 'interfaces', 'uni' → 'unit', 'fam' → 'family'
        kws = complete("set int eth0 uni 0 fam", CLIMode.CONFIGURE)
        assert "family" in kws

    def test_abbreviated_set_deep_walk_with_space(self):
        # After fully resolving abbreviated walk, show family children
        kws = complete("set int eth0 uni 0 family ", CLIMode.CONFIGURE)
        assert "inet" in kws
        assert "inet6" in kws

    def test_abbreviated_set_intermediate_walk(self):
        # 'set sys ' should walk into system node
        kws = complete("set sys ", CLIMode.CONFIGURE)
        assert "host-name" in kws
        assert "ntp" in kws

    def test_abbreviated_configure_command_dispatches(self):
        # 'del ' should show config tree (same as 'delete ')
        kws = complete("del ", CLIMode.CONFIGURE)
        assert "interfaces" in kws
        assert "system" in kws

    def test_abbreviated_edit_dispatches(self):
        kws = complete("ed ", CLIMode.CONFIGURE)
        assert "interfaces" in kws

    def test_abbreviated_show_in_configure_dispatches(self):
        # 'sho ' in configure mode should show config tree sections
        kws = complete("sho ", CLIMode.CONFIGURE)
        assert "interfaces" in kws

    def test_ambiguous_configure_command_yields_nothing(self):
        # 's' matches 'set' and 'show' — ambiguous → no subcommand completions
        kws = complete("s ", CLIMode.CONFIGURE)
        assert "interfaces" not in kws
        assert "host-name" not in kws

    def test_abbreviated_rollback_dispatches(self):
        kws = complete("rol ", CLIMode.CONFIGURE)
        assert any("<0-49>" in k for k in kws)

    def test_abbreviated_commit_dispatches(self):
        kws = complete("com ", CLIMode.CONFIGURE)
        assert "confirmed" in kws
        assert "check" in kws

    # Config walk abbreviation via complete_config_tokens directly
    def test_complete_config_abbreviated_section(self):
        # 'sys' should resolve to 'system' during walk
        results = complete_config_tokens(["sys"], True, [])
        kws = [c.text for c in results]
        assert "host-name" in kws
        assert "ntp" in kws

    def test_complete_config_abbreviated_nested_walk(self):
        # 'pro' → 'protocols', 'bg' → 'bgp'
        results = complete_config_tokens(["pro", "bg"], True, [])
        kws = [c.text for c in results]
        assert "group" in kws

    def test_complete_config_ambiguous_walk_returns_empty(self):
        # 'r' matches 'routing-options' and 'routing-instances' → ambiguous walk → no completions
        results = complete_config_tokens(["r"], True, [])
        assert results == []
