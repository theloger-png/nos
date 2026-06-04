"""Tests for the policy-statement unnamed final term feature.

JunOS allows a policy-statement to carry a direct "then" action (no term name)
that acts as a catch-all evaluated after all named terms:

    set policy-options policy-statement MY-POLICY then accept
    set policy-options policy-statement MY-POLICY then reject
    set policy-options policy-statement MY-POLICY then next-policy
"""
from __future__ import annotations

import pytest

from nos.cli.completer import (
    CONFIG_TREE,
    NOSCompleter,
    build_config_tree,
    complete_config_tokens,
    navigate_tree,
)
from nos.cli.parser import CLIMode
from nos.config.schema import FinalTermAction, NOSConfig, PolicyStatement
from nos.config.serializer import from_set_commands, to_set_commands
from nos.config.validator import ConfigValidator
from nos.drivers.frr.renderer import FRRRenderer


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _complete(text: str) -> list[str]:
    """Return keyword completions for a configure-mode 'set ...' line."""
    completing_new = text.endswith(" ")
    tokens = text.split()
    # Strip "set" prefix, pass the rest to the config completer.
    rest = tokens[1:] if tokens and tokens[0] == "set" else tokens
    c = NOSCompleter(mode=CLIMode.CONFIGURE, edit_path=[])
    return [cp.text for cp in complete_config_tokens(rest, completing_new, [])]


def _set(cmd_suffix: str) -> dict:
    """Run from_set_commands for 'set <cmd_suffix>' and return the resulting dict."""
    return from_set_commands([f"set {cmd_suffix}"])


# ===========================================================================
# Schema
# ===========================================================================

class TestFinalTermActionModel:
    def test_accepts_accept(self):
        m = FinalTermAction(accept=True)
        assert m.accept is True
        assert m.reject is False
        assert m.next_policy is False

    def test_accepts_reject(self):
        m = FinalTermAction(reject=True)
        assert m.accept is False
        assert m.reject is True

    def test_accepts_next_policy(self):
        m = FinalTermAction(next_policy=True)
        assert m.next_policy is True

    def test_defaults_all_false(self):
        m = FinalTermAction()
        assert not m.accept and not m.reject and not m.next_policy


class TestPolicyStatementSchema:
    def test_statement_has_then_field(self):
        ps = PolicyStatement()
        assert ps.then is None

    def test_statement_accepts_final_action(self):
        ps = PolicyStatement.model_validate({"then": {"accept": True}})
        assert ps.then is not None
        assert ps.then.accept is True

    def test_statement_named_terms_unchanged(self):
        ps = PolicyStatement.model_validate(
            {"term": {"T1": {"from_config": None, "then_config": {"accept": True}}}}
        )
        assert "T1" in ps.term
        assert ps.then is None

    def test_nos_config_validates_final_term_accept(self):
        config = {
            "policy_options": {
                "policy_statement": {
                    "MY-POLICY": {"then": {"accept": True}}
                }
            }
        }
        result = ConfigValidator().validate(config)
        assert result.is_valid

    def test_nos_config_validates_final_term_reject(self):
        config = {
            "policy_options": {
                "policy_statement": {
                    "MY-POLICY": {"then": {"reject": True}}
                }
            }
        }
        assert ConfigValidator().validate(config).is_valid

    def test_nos_config_validates_final_term_next_policy(self):
        config = {
            "policy_options": {
                "policy_statement": {
                    "MY-POLICY": {"then": {"next_policy": True}}
                }
            }
        }
        assert ConfigValidator().validate(config).is_valid

    def test_final_term_coexists_with_named_terms(self):
        config = {
            "policy_options": {
                "prefix_list": {"ALL": ["0.0.0.0/0"]},
                "policy_statement": {
                    "MY-POLICY": {
                        "term": {
                            "T1": {
                                "from_config": {"protocol": "bgp"},
                                "then_config": {"accept": True},
                            }
                        },
                        "then": {"reject": True},
                    }
                },
            }
        }
        assert ConfigValidator().validate(config).is_valid


# ===========================================================================
# Serializer — from_set_commands
# ===========================================================================

class TestFinalTermParsing:
    # Note: from_set_commands converts path-component hyphens to underscores
    # (e.g. "MYPOL" has no hyphens so it is stored as-is).  Policy names with
    # hyphens would be stored as underscore variants — that is a pre-existing
    # codebase behaviour unrelated to this feature.

    def test_set_then_accept(self):
        result = _set("policy-options policy-statement MYPOL then accept")
        assert result["policy_options"]["policy_statement"]["MYPOL"]["then"]["accept"] is True

    def test_set_then_reject(self):
        result = _set("policy-options policy-statement MYPOL then reject")
        assert result["policy_options"]["policy_statement"]["MYPOL"]["then"]["reject"] is True

    def test_set_then_next_policy(self):
        result = _set("policy-options policy-statement MYPOL then next-policy")
        then = result["policy_options"]["policy_statement"]["MYPOL"]["then"]
        assert then.get("next_policy") is True

    def test_named_term_then_accept_unchanged(self):
        result = _set("policy-options policy-statement MYPOL term T1 then accept")
        term = result["policy_options"]["policy_statement"]["MYPOL"]["term"]["T1"]
        assert term["then"]["accept"] is True

    def test_named_term_from_protocol_unchanged(self):
        result = _set('policy-options policy-statement MYPOL term T1 from protocol "bgp"')
        term = result["policy_options"]["policy_statement"]["MYPOL"]["term"]["T1"]
        assert term["from"]["protocol"] == "bgp"


# ===========================================================================
# Serializer — to_set_commands (show configuration | display set)
# ===========================================================================

class TestFinalTermDisplay:
    def test_to_set_accept(self):
        config = {
            "policy_options": {
                "policy_statement": {
                    "MY-POLICY": {"then": {"accept": True}}
                }
            }
        }
        cmds = to_set_commands(config)
        assert any(c == "set policy-options policy-statement MY-POLICY then accept" for c in cmds)

    def test_to_set_reject(self):
        config = {
            "policy_options": {
                "policy_statement": {
                    "MY-POLICY": {"then": {"reject": True}}
                }
            }
        }
        cmds = to_set_commands(config)
        assert any(c == "set policy-options policy-statement MY-POLICY then reject" for c in cmds)

    def test_to_set_next_policy(self):
        config = {
            "policy_options": {
                "policy_statement": {
                    "MY-POLICY": {"then": {"next_policy": True}}
                }
            }
        }
        cmds = to_set_commands(config)
        assert any(c == "set policy-options policy-statement MY-POLICY then next-policy" for c in cmds)

    def test_roundtrip_accept(self):
        config = {
            "policy_options": {
                "policy_statement": {"P": {"then": {"accept": True}}}
            }
        }
        assert to_set_commands(from_set_commands(to_set_commands(config))) == to_set_commands(config)

    def test_roundtrip_reject(self):
        config = {
            "policy_options": {
                "policy_statement": {"P": {"then": {"reject": True}}}
            }
        }
        assert to_set_commands(from_set_commands(to_set_commands(config))) == to_set_commands(config)

    def test_display_only_set_true_booleans(self):
        """False booleans must not appear in the set output."""
        config = {
            "policy_options": {
                "policy_statement": {
                    "MY-POLICY": {"then": {"accept": True, "reject": False, "next_policy": False}}
                }
            }
        }
        cmds = to_set_commands(config)
        assert not any("reject" in c for c in cmds)
        assert not any("next-policy" in c for c in cmds)
        assert any("accept" in c for c in cmds)


# ===========================================================================
# Completer — tab completion
# ===========================================================================

class TestFinalTermCompletion:
    def test_then_offered_at_policy_statement_level(self):
        # "set policy-options policy-statement MY-POLICY " should offer "then"
        kws = _complete("set policy-options policy-statement MY-POLICY ")
        assert "then" in kws

    def test_term_still_offered_at_policy_statement_level(self):
        kws = _complete("set policy-options policy-statement MY-POLICY ")
        assert "term" in kws

    def test_then_prefix_completes_to_then(self):
        kws = _complete("set policy-options policy-statement MY-POLICY th")
        assert "then" in kws

    def test_then_actions_offered(self):
        kws = _complete("set policy-options policy-statement MY-POLICY then ")
        assert "accept" in kws
        assert "reject" in kws
        assert "next-policy" in kws

    def test_accept_partial_completes(self):
        kws = _complete("set policy-options policy-statement MY-POLICY then ac")
        assert "accept" in kws

    def test_next_policy_partial_completes(self):
        kws = _complete("set policy-options policy-statement MY-POLICY then next")
        assert "next-policy" in kws

    def test_config_tree_ps_inner_has_then(self):
        po = CONFIG_TREE.children["policy-options"]
        ps_dyn = po.children["policy-statement"]
        ps_inner = ps_dyn.dynamic_child
        assert ps_inner is not None
        assert "then" in ps_inner.children

    def test_then_node_has_three_actions(self):
        po = CONFIG_TREE.children["policy-options"]
        ps_inner = po.children["policy-statement"].dynamic_child
        then_node = ps_inner.children["then"]
        assert "accept" in then_node.children
        assert "reject" in then_node.children
        assert "next-policy" in then_node.children

    def test_actions_are_presence_nodes(self):
        po = CONFIG_TREE.children["policy-options"]
        ps_inner = po.children["policy-statement"].dynamic_child
        then_node = ps_inner.children["then"]
        for action in ("accept", "reject", "next-policy"):
            assert then_node.children[action].is_presence, f"{action} should be a presence node"


# ===========================================================================
# FRR renderer
# ===========================================================================

@pytest.fixture()
def renderer():
    return FRRRenderer()


class TestFinalTermRendering:
    def test_accept_renders_permit_65535(self, renderer):
        config = {
            "policy_options": {
                "policy_statement": {
                    "MY-POLICY": {"then": {"accept": True}}
                }
            }
        }
        out = renderer.render(config)
        assert "route-map MY-POLICY permit 65535" in out

    def test_reject_renders_deny_65535(self, renderer):
        config = {
            "policy_options": {
                "policy_statement": {
                    "MY-POLICY": {"then": {"reject": True}}
                }
            }
        }
        out = renderer.render(config)
        assert "route-map MY-POLICY deny 65535" in out

    def test_next_policy_renders_no_entry(self, renderer):
        config = {
            "policy_options": {
                "policy_statement": {
                    "MY-POLICY": {"then": {"next_policy": True}}
                }
            }
        }
        out = renderer.render(config)
        assert "route-map MY-POLICY" not in out

    def test_no_policy_options_no_route_map(self, renderer):
        out = renderer.render({})
        assert "route-map" not in out

    def test_final_term_after_named_terms(self, renderer):
        config = {
            "policy_options": {
                "policy_statement": {
                    "MY-POLICY": {
                        "term": {
                            "T1": {
                                "from": {"protocol": "bgp"},
                                "then": {"accept": True},
                            }
                        },
                        "then": {"reject": True},
                    }
                }
            }
        }
        out = renderer.render(config)
        permit_pos = out.index("route-map MY-POLICY permit 10")
        deny_pos = out.index("route-map MY-POLICY deny 65535")
        assert permit_pos < deny_pos, "named term (permit 10) must precede final term (deny 65535)"

    def test_named_term_protocol_match(self, renderer):
        config = {
            "policy_options": {
                "policy_statement": {
                    "EXPORT": {
                        "term": {
                            "T1": {
                                "from": {"protocol": "static"},
                                "then": {"accept": True},
                            }
                        }
                    }
                }
            }
        }
        out = renderer.render(config)
        assert "route-map EXPORT permit 10" in out
        assert "match source-protocol static" in out

    def test_named_term_direct_maps_to_connected(self, renderer):
        config = {
            "policy_options": {
                "policy_statement": {
                    "P": {
                        "term": {
                            "T1": {
                                "from": {"protocol": "direct"},
                                "then": {"accept": True},
                            }
                        }
                    }
                }
            }
        }
        out = renderer.render(config)
        assert "match source-protocol connected" in out

    def test_named_term_without_final_term(self, renderer):
        """Named-term-only policy should have no 65535 entry."""
        config = {
            "policy_options": {
                "policy_statement": {
                    "P": {
                        "term": {
                            "T1": {"from": {"protocol": "bgp"}, "then": {"accept": True}}
                        }
                    }
                }
            }
        }
        out = renderer.render(config)
        assert "65535" not in out

    def test_schema_format_also_renders(self, renderer):
        """Renderer handles from_config/then_config (schema-format) named terms."""
        config = {
            "policy_options": {
                "policy_statement": {
                    "P": {
                        "term": {
                            "T1": {
                                "from_config": {"protocol": "bgp"},
                                "then_config": {"accept": True},
                            }
                        },
                        "then": {"reject": True},
                    }
                }
            }
        }
        out = renderer.render(config)
        assert "route-map P permit 10" in out
        assert "route-map P deny 65535" in out

    def test_route_maps_precede_bgp_block(self, renderer):
        config = {
            "routing_options": {"autonomous_system": 65000, "router_id": "1.1.1.1"},
            "protocols": {
                "bgp": {
                    "group": {
                        "IBGP": {
                            "group_type": "internal",
                            "local_address": "1.1.1.1",
                            "neighbor": {"2.2.2.2": {}},
                            "export": "MY-POLICY",
                        }
                    }
                }
            },
            "policy_options": {
                "policy_statement": {
                    "MY-POLICY": {"then": {"accept": True}}
                }
            },
        }
        out = renderer.render(config)
        route_map_pos = out.index("route-map MY-POLICY permit 65535")
        bgp_pos = out.index("router bgp 65000")
        assert route_map_pos < bgp_pos, "route-map must appear before the BGP block"

    def test_multiple_policies_all_rendered(self, renderer):
        config = {
            "policy_options": {
                "policy_statement": {
                    "ACCEPT-ALL": {"then": {"accept": True}},
                    "REJECT-ALL": {"then": {"reject": True}},
                }
            }
        }
        out = renderer.render(config)
        assert "route-map ACCEPT-ALL permit 65535" in out
        assert "route-map REJECT-ALL deny 65535" in out
