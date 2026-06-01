"""Tab completion and ? help for NOS CLI.

Provides context-aware completions for both operational and configure modes.
The JunOS config hierarchy is modelled as a ConfigNode tree; completion
walks that tree based on tokens already typed and the current edit path.
"""
from __future__ import annotations

import shlex
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Generator, Optional

from prompt_toolkit.completion import CompleteEvent, Completion, Completer
from prompt_toolkit.document import Document

from nos.cli.parser import CLIMode

if TYPE_CHECKING:
    from nos.config.store import ConfigStore


# ============================================================================
# Config tree model
# ============================================================================

@dataclass
class ConfigNode:
    """One node in the JunOS configuration hierarchy."""

    help: str = ""
    # Static keyword children
    children: dict[str, "ConfigNode"] = field(default_factory=dict)
    # Accepts any user-defined name (interface name, vlan name, IP, …)
    dynamic_child: Optional["ConfigNode"] = None
    dynamic_hint: str = "<name>"
    # True → the NEXT token after this keyword is a plain value (not a sub-key)
    is_value: bool = False
    value_hint: str = "<value>"
    enum_choices: list[str] = field(default_factory=list)
    # True → no value token follows; setting this path is the action
    is_presence: bool = False


# ── tree builder helpers ────────────────────────────────────────────────────

def _v(help: str, hint: str = "<string>") -> ConfigNode:
    """Value leaf: next token is a plain string / number."""
    return ConfigNode(help=help, is_value=True, value_hint=hint)


def _e(help: str, choices: list[str]) -> ConfigNode:
    """Enum leaf: next token must be one of *choices*."""
    return ConfigNode(
        help=help,
        is_value=True,
        value_hint=f"[{'|'.join(choices)}]",
        enum_choices=list(choices),
    )


def _p(help: str) -> ConfigNode:
    """Presence flag: no value token, path itself is the flag."""
    return ConfigNode(help=help, is_presence=True)


def _n(help: str, children: dict[str, ConfigNode]) -> ConfigNode:
    """Internal keyword node with static children."""
    return ConfigNode(help=help, children=children)


def _d(help: str, hint: str, child: ConfigNode) -> ConfigNode:
    """Dynamic node: the next token is a user-defined name."""
    n = ConfigNode(help=help)
    n.dynamic_child = child
    n.dynamic_hint = hint
    return n


# ── tree construction ───────────────────────────────────────────────────────

def build_config_tree() -> ConfigNode:
    """Return the root ConfigNode of the full JunOS config hierarchy."""

    # ── system ─────────────────────────────────────────────────────────────
    system_node = _n("System parameters", {
        "host-name": _v("System hostname"),
        "domain-name": _v("DNS domain name"),
        "name-server": _v("DNS server address", "<ip-address>"),
        "ntp": _n("NTP configuration", {
            "server": _v("NTP server address", "<ip-address>"),
        }),
        "login": _n("Login configuration", {
            "user": _d("User accounts", "<username>", _n("User account", {
                "class": _e("User class", ["super-user", "operator", "read-only"]),
                "authentication": _n("Authentication methods", {
                    "plain-text-password": _v("Plain-text password"),
                    "ssh-rsa": _v("SSH RSA public key", "<key>"),
                }),
            })),
        }),
        "syslog": _n("System logging", {
            "file": _d("Log file", "<filename>", _n("Log file config", {
                "any": _e("Severity level", [
                    "emergency", "alert", "critical", "error",
                    "warning", "notice", "info", "debug", "any",
                ]),
            })),
        }),
    })

    # ── interfaces ─────────────────────────────────────────────────────────
    inet_addr_node = _n("IPv4 address configuration", {
        "primary": _p("Set as primary address"),
    })

    unit_inner = _n("Logical interface unit", {
        "family": _n("Protocol family", {
            "inet": _n("IPv4 family", {
                "address": _d("IPv4 address", "<ip/prefix>", inet_addr_node),
            }),
            "inet6": _n("IPv6 family", {
                "address": _d("IPv6 address", "<ipv6/prefix>",
                               ConfigNode(help="IPv6 address/prefix")),
            }),
            "ethernet-switching": _n("Ethernet switching", {
                "interface-mode": _e("Port mode", ["access", "trunk"]),
                "vlan": _n("VLAN membership", {
                    "members": _v("VLAN name, ID (1-4094), or 'all'",
                                  "<vlan-name>|all|<1-4094>"),
                }),
            }),
        }),
    })

    interface_inner = _n("Interface configuration", {
        "description": _v("Interface description"),
        "mtu": _v("Maximum transmission unit", "<256-9192>"),
        "speed": _e("Link speed",
                     ["auto", "10m", "100m", "1g", "10g", "25g", "40g", "100g"]),
        "duplex": _e("Link duplex", ["auto", "half", "full"]),
        "disable": _p("Administratively disable this interface"),
        "family": _n("Protocol family (routed port)", {
            "inet": _n("IPv4 family", {
                "address": _d("IPv4 address", "<ip/prefix>", inet_addr_node),
            }),
            "inet6": _n("IPv6 family", {
                "address": _d("IPv6 address", "<ipv6/prefix>",
                               ConfigNode(help="IPv6 address/prefix")),
            }),
        }),
        "unit": _d("Logical unit number", "<0-16384>", unit_inner),
    })

    interfaces_node = _d("Physical/logical interfaces", "<interface-name>", interface_inner)

    # ── vlans ──────────────────────────────────────────────────────────────
    vlan_inner = _n("VLAN definition", {
        "vlan-id": _v("VLAN ID", "<1-4094>"),
        "description": _v("VLAN description"),
        "l3-interface": _v("SVI interface (e.g. irb.100)", "irb.<vlan-id>"),
    })
    vlans_node = _d("VLAN definitions", "<vlan-name>", vlan_inner)

    # ── routing-options ────────────────────────────────────────────────────
    route_inner = _n("Static route configuration", {
        "next-hop": _v("Next-hop IP address", "<ip-address>"),
        "discard": _p("Silently discard packets"),
        "reject": _p("Reject with ICMP unreachable"),
    })

    routing_options_node = _n("Routing options", {
        "static": _n("Static routes", {
            "route": _d("Destination prefix", "<ip-prefix>", route_inner),
        }),
        "router-id": _v("Router ID", "<ip-address>"),
        "autonomous-system": _v("BGP AS number", "<1-4294967295>"),
    })

    # ── protocols ──────────────────────────────────────────────────────────
    isis_iface_inner = _n("IS-IS interface configuration", {
        "point-to-point": _p("Point-to-point link"),
        "hello-interval": _v("Hello interval", "<seconds>"),
        "hold-time": _v("Hold time", "<seconds>"),
        "level": _n("Level configuration", {
            "1": _n("Level 1", {"disable": _p("Disable IS-IS level 1")}),
            "2": _n("Level 2", {"disable": _p("Disable IS-IS level 2")}),
        }),
    })

    bgp_neighbor_inner = _n("BGP neighbor", {
        "description": _v("Neighbor description"),
        "authentication-key": _v("MD5 authentication key"),
        "hold-time": _v("Hold time", "<seconds>"),
    })

    bgp_group_inner = _n("BGP peer group", {
        "type": _e("Group type", ["internal", "external"]),
        "local-as": _v("Local AS number", "<asn>"),
        "peer-as": _v("Peer AS number (eBGP)", "<asn>"),
        "local-address": _v("Local BGP address", "<ip-address>"),
        "neighbor": _d("BGP neighbor", "<ip-address>", bgp_neighbor_inner),
        "export": _v("Export policy name", "<policy-name>"),
        "import": _v("Import policy name", "<policy-name>"),
        "family": _n("Address family", {
            "inet": _n("IPv4", {"unicast": _p("IPv4 unicast")}),
            "inet6": _n("IPv6", {"unicast": _p("IPv6 unicast")}),
        }),
    })

    protocols_node = _n("Routing protocols", {
        "isis": _n("IS-IS protocol", {
            "interface": _d("IS-IS interface", "<interface-name>", isis_iface_inner),
            "level": _n("IS-IS level parameters", {
                "1": _n("Level 1", {"wide-metrics-only": _p("Use wide metrics only")}),
                "2": _n("Level 2", {"wide-metrics-only": _p("Use wide metrics only")}),
            }),
        }),
        "bgp": _n("BGP protocol", {
            "group": _d("BGP peer group", "<group-name>", bgp_group_inner),
        }),
    })

    # ── policy-options ─────────────────────────────────────────────────────
    pl_inner = ConfigNode(help="Prefix list")
    pl_inner.dynamic_child = _p("IP prefix entry")
    pl_inner.dynamic_hint = "<ip-prefix>"

    ps_term_inner = _n("Policy term", {
        "from": _n("Match conditions", {
            "prefix-list": _v("Prefix list name", "<prefix-list-name>"),
            "protocol": _e("Protocol", ["bgp", "isis", "ospf", "static", "direct"]),
            "route-filter": _v("Route filter", "<prefix> <match-type>"),
        }),
        "then": _n("Actions", {
            "accept": _p("Accept the route"),
            "reject": _p("Reject the route"),
            "next-hop": _v("Override next-hop", "<ip-address>"),
            "local-preference": _v("Set local preference", "<0-4294967295>"),
            "metric": _v("Set metric", "<value>"),
            "community": _n("Community actions", {
                "add": _v("Add community", "<community>"),
            }),
        }),
    })

    ps_inner = _n("Policy statement", {
        "term": _d("Policy term", "<term-name>", ps_term_inner),
    })

    policy_options_node = _n("Policy options", {
        "prefix-list": _d("Prefix lists", "<prefix-list-name>", pl_inner),
        "policy-statement": _d("Routing policies", "<policy-name>", ps_inner),
    })

    # ── routing-instances ─────────────────────────────────────────────────
    ri_inner = _n("Routing instance", {
        "instance-type": _e("Instance type", ["vrf", "virtual-router"]),
        "interface": _d("Assigned interfaces", "<interface-name>",
                         _p("Interface assignment")),
        "route-distinguisher": _v("Route distinguisher", "<rd>"),
        "vrf-target": _v("VRF target RT", "<rt>"),
        "routing-options": _n("Per-instance routing options", {
            "static": _n("Static routes", {
                "route": _d("Prefix", "<ip-prefix>", _n("Route", {
                    "next-hop": _v("Next hop", "<ip-address>"),
                })),
            }),
        }),
    })

    routing_instances_node = _d("Routing instances (VRF/VR)", "<instance-name>", ri_inner)

    return _n("Configuration root", {
        "system": system_node,
        "interfaces": interfaces_node,
        "vlans": vlans_node,
        "routing-options": routing_options_node,
        "protocols": protocols_node,
        "policy-options": policy_options_node,
        "routing-instances": routing_instances_node,
    })


# singleton
CONFIG_TREE: ConfigNode = build_config_tree()


# ============================================================================
# Tree navigation
# ============================================================================

def navigate_tree(root: ConfigNode, path: list[str]) -> Optional[ConfigNode]:
    """Walk *root* following *path* tokens; return node reached or None."""
    node: Optional[ConfigNode] = root
    for part in path:
        assert node is not None
        if part in node.children:
            node = node.children[part]
        elif node.dynamic_child is not None:
            node = node.dynamic_child
        else:
            return None
    return node


def _candidate_keys(store: "ConfigStore", jpath: list[str]) -> list[str]:
    """Return JunOS-format keys at *jpath* in the candidate config."""
    try:
        cfg = store.get_candidate()
        cur: object = cfg
        for part in jpath:
            internal = part.replace("-", "_")
            if isinstance(cur, dict) and internal in cur:
                cur = cur[internal]
            else:
                return []
        if isinstance(cur, dict):
            return [k.replace("_", "-") for k in cur]
    except Exception:
        pass
    return []


# ============================================================================
# Completion helpers
# ============================================================================

def _completions_at_node(
    node: ConfigNode,
    prefix: str,
    store: Optional["ConfigStore"],
    path_so_far: list[str],
) -> list[Completion]:
    """Return Completions for what can follow *node* given *prefix*."""
    results: list[Completion] = []

    if node.is_value:
        if node.enum_choices:
            for choice in node.enum_choices:
                if choice.startswith(prefix):
                    results.append(
                        Completion(choice, -len(prefix), display_meta=node.help)
                    )
        else:
            hint = node.value_hint
            results.append(
                Completion(hint, -len(prefix), display_meta=node.help)
            )
        return results

    # Static keyword children
    for kw, child in sorted(node.children.items()):
        if kw.startswith(prefix):
            results.append(
                Completion(kw, -len(prefix), display_meta=child.help)
            )

    # Dynamic child: real values from config + hint
    if node.dynamic_child is not None:
        if store is not None:
            for val in _candidate_keys(store, path_so_far):
                if val.startswith(prefix):
                    results.append(
                        Completion(val, -len(prefix),
                                   display_meta=node.dynamic_child.help)
                    )
        hint = node.dynamic_hint
        if not prefix or hint.startswith(prefix):
            results.append(
                Completion(hint, -len(prefix),
                           display_meta=node.dynamic_child.help)
            )

    return results


def complete_config_tokens(
    tokens: list[str],
    completing_new: bool,
    edit_path: list[str],
    store: Optional["ConfigStore"] = None,
) -> list[Completion]:
    """Return completions for tokens typed after set / delete / edit.

    *edit_path* is the current hierarchy position (JunOS hyphen format).
    *completing_new* is True when the cursor follows a trailing space.
    """
    node = navigate_tree(CONFIG_TREE, edit_path)
    if node is None:
        return []

    prefix = "" if completing_new else (tokens[-1] if tokens else "")
    walk_tokens = tokens if completing_new else tokens[:-1]

    walked: list[str] = list(edit_path)
    for token in walk_tokens:
        if node.is_value or node.is_presence:
            return []
        if token in node.children:
            walked.append(token)
            node = node.children[token]
        elif node.dynamic_child is not None:
            walked.append(token)
            node = node.dynamic_child
        else:
            return []

    return _completions_at_node(node, prefix, store, walked)


# ============================================================================
# Main completer
# ============================================================================

_OPERATIONAL_CMDS = {
    "show": "Display system information",
    "ping": "Send ICMP echo request",
    "traceroute": "Trace route to a host",
    "configure": "Enter configure mode",
    "exit": "Exit this session",
    "quit": "Exit this session",
}

_SHOW_OPER_ARGS = {
    "interfaces": "Show interface status and counters",
    "route": "Show routing table",
    "bgp": "Show BGP information",
    "isis": "Show IS-IS information",
    "vlans": "Show VLAN table",
    "system": "Show system information",
    "forwarding": "Show PFE forwarding mode",
    "configuration": "Show running configuration as set commands",
}

_CONFIGURE_CMDS = {
    "set": "Set a configuration parameter",
    "delete": "Delete a configuration element",
    "edit": "Navigate into a configuration level",
    "up": "Go up one level in the hierarchy",
    "top": "Return to the top configuration level",
    "show": "Show candidate configuration",
    "commit": "Apply candidate configuration",
    "rollback": "Revert to a previous checkpoint",
    "discard": "Discard all candidate changes",
    "run": "Run an operational command",
    "exit": "Return to operational mode",
    "quit": "Return to operational mode",
}


class NOSCompleter(Completer):
    """Context-aware completer for the NOS JunOS-like CLI."""

    def __init__(
        self,
        mode: CLIMode,
        edit_path: list[str],
        store: Optional["ConfigStore"] = None,
    ) -> None:
        self.mode = mode
        self.edit_path: list[str] = edit_path
        self.store = store

    def get_completions(
        self, document: Document, complete_event: CompleteEvent
    ) -> Generator[Completion, None, None]:
        text = document.text_before_cursor
        completing_new = text.endswith(" ")

        try:
            tokens = shlex.split(text)
        except ValueError:
            tokens = text.split()

        if not tokens or (len(tokens) == 1 and not completing_new):
            # Completing the command keyword itself
            prefix = tokens[0] if tokens else ""
            yield from self._complete_command_keyword(prefix)
            return

        cmd = tokens[0].lower()
        rest = tokens[1:]

        if self.mode == CLIMode.OPERATIONAL:
            yield from self._complete_operational(cmd, rest, completing_new)
        else:
            yield from self._complete_configure(cmd, rest, completing_new)

    # ------------------------------------------------------------------
    # Command keyword completions
    # ------------------------------------------------------------------

    def _complete_command_keyword(self, prefix: str) -> Generator[Completion, None, None]:
        table = _OPERATIONAL_CMDS if self.mode == CLIMode.OPERATIONAL else _CONFIGURE_CMDS
        for kw, desc in table.items():
            if kw.startswith(prefix):
                yield Completion(kw, -len(prefix), display_meta=desc)

    # ------------------------------------------------------------------
    # Operational mode
    # ------------------------------------------------------------------

    def _complete_operational(
        self, cmd: str, rest: list[str], completing_new: bool
    ) -> Generator[Completion, None, None]:
        if cmd == "show":
            yield from self._complete_show_operational(rest, completing_new)
        elif cmd == "ping":
            if not rest or (len(rest) == 1 and not completing_new):
                yield Completion("<host>", display_meta="Hostname or IP address")
        elif cmd == "traceroute":
            if not rest or (len(rest) == 1 and not completing_new):
                yield Completion("<host>", display_meta="Hostname or IP address")

    def _complete_show_operational(
        self, rest: list[str], completing_new: bool
    ) -> Generator[Completion, None, None]:
        prefix = "" if completing_new else (rest[-1] if rest else "")

        if not rest or (len(rest) == 1 and not completing_new):
            # First token after "show": operational sub-commands
            for kw, desc in _SHOW_OPER_ARGS.items():
                if kw.startswith(prefix):
                    yield Completion(kw, -len(prefix), display_meta=desc)
            if "|".startswith(prefix):
                yield Completion("|", -len(prefix), display_meta="Filter output")
            return

        # "show configuration <section-path>": complete against config tree
        if rest[0].lower() == "configuration":
            yield from complete_config_tokens(
                rest[1:], completing_new, [], self.store
            )

    # ------------------------------------------------------------------
    # Configure mode
    # ------------------------------------------------------------------

    def _complete_configure(
        self, cmd: str, rest: list[str], completing_new: bool
    ) -> Generator[Completion, None, None]:
        if cmd in ("set", "delete", "edit"):
            yield from complete_config_tokens(
                rest, completing_new, self.edit_path, self.store
            )
        elif cmd == "commit":
            yield from self._complete_commit(rest, completing_new)
        elif cmd == "rollback":
            yield from self._complete_rollback(rest, completing_new)
        elif cmd == "run":
            # Delegate to operational completions
            if rest:
                sub_cmd = rest[0].lower()
                yield from self._complete_operational(sub_cmd, rest[1:], completing_new)
            else:
                for kw, desc in _OPERATIONAL_CMDS.items():
                    if kw != "configure":
                        yield Completion(kw, display_meta=desc)
        elif cmd == "show":
            yield from self._complete_show_configure(rest, completing_new)
        elif cmd == "up":
            if not rest or (len(rest) == 1 and not completing_new):
                prefix = rest[0] if rest and not completing_new else ""
                yield Completion("<count>", -len(prefix), display_meta="Number of levels to go up")

    def _complete_show_configure(
        self, rest: list[str], completing_new: bool
    ) -> Generator[Completion, None, None]:
        # After "|": only "compare" (and other pipe filters in the future)
        if "|" in rest:
            pipe_idx = rest.index("|")
            after = rest[pipe_idx + 1:]
            prefix = "" if completing_new else (after[-1] if after else "")
            if "compare".startswith(prefix):
                yield Completion("compare", -len(prefix),
                                 display_meta="Compare with running config")
            return

        # Config section path completions, relative to the current edit_path.
        # Reuses the same tree-walk as set/delete/edit so the user sees the
        # same hierarchy (e.g. "show interfaces", "show protocols bgp").
        yield from complete_config_tokens(
            rest, completing_new, self.edit_path, self.store
        )

        # Offer "|" whenever the cursor is at a fresh token boundary so the
        # user can pipe the output (e.g. "show interfaces | match eth").
        if completing_new:
            yield Completion("|", display_meta="Filter output")

    def _complete_commit(
        self, rest: list[str], completing_new: bool
    ) -> Generator[Completion, None, None]:
        prefix = "" if completing_new else (rest[-1] if rest else "")
        if not rest or (len(rest) == 1 and not completing_new):
            for kw, desc in [("confirmed", "Commit with auto-rollback timer"),
                              ("check", "Validate without applying")]:
                if kw.startswith(prefix):
                    yield Completion(kw, -len(prefix), display_meta=desc)
        elif rest[0] == "confirmed" and (len(rest) == 1 and completing_new or
                                          len(rest) == 2 and not completing_new):
            yield Completion("<minutes>", display_meta="Auto-rollback timeout in minutes")

    def _complete_rollback(
        self, rest: list[str], completing_new: bool
    ) -> Generator[Completion, None, None]:
        if not rest or (len(rest) == 1 and not completing_new):
            prefix = rest[0] if rest and not completing_new else ""
            yield Completion("<0-49>", -len(prefix), display_meta="Rollback checkpoint number")
