"""Tab completion and ? help for NOS CLI.

Provides context-aware completions for both operational and configure modes.
The JunOS config hierarchy is modelled as a ConfigNode tree; completion
walks that tree based on tokens already typed and the current edit path.
"""
from __future__ import annotations

import re
import shlex
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Generator, Optional

from prompt_toolkit.completion import CompleteEvent, Completion, Completer
from prompt_toolkit.document import Document

from nos.cli.parser import CLIMode, resolve_prefix
from nos.config.serializer import _merge_compound_tokens

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
    # True → token matching <name>.<digits> is expanded to <name> unit <digits>
    expand_dotted_unit: bool = False


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
        "family": _n("Address family", {
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
        "vlan-id": _v("802.1Q VLAN ID for this unit", "<1-4094>"),
    })

    interface_inner = _n("Interface configuration", {
        "description": _v("Interface description"),
        "mtu": _v("Maximum transmission unit", "<256-9192>"),
        "speed": _e("Link speed",
                     ["auto", "10m", "100m", "1g", "10g", "25g", "40g", "100g"]),
        "duplex": _e("Link duplex", ["auto", "half", "full"]),
        "disable": _p("Administratively disable this interface"),
        "family": _n("Address family", {
            "inet": _n("IPv4 family (routed port)", {
                "address": _d("IPv4 address", "<ip/prefix>", inet_addr_node),
            }),
            "inet6": _n("IPv6 family (routed port)", {
                "address": _d("IPv6 address", "<ipv6/prefix>",
                               ConfigNode(help="IPv6 address/prefix")),
            }),
        }),
        "unit": _d("Logical unit number", "<0-16384>", unit_inner),
    })

    interfaces_node = _d("Physical/logical interfaces", "<interface-name>", interface_inner)
    interfaces_node.expand_dotted_unit = True

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

# Matches "ens34.0", "irb.100", "eth0.10", etc. — <name>.<unit-number>
_DOTTED_UNIT_RE = re.compile(r'^([^.]+)\.(\d+)$')


def _advance_past_unit(iface_inner: ConfigNode, unit_str: str) -> ConfigNode:
    """Return the tree node reached after consuming <iface> unit <unit-str>.

    Starting from *iface_inner* (the node for an interface name's content),
    navigate unit → <unit-str> and return the unit content node.  If the
    tree doesn't have the expected structure, return *iface_inner* unchanged
    so the caller degrades gracefully.
    """
    unit_dyn = iface_inner.children.get("unit")
    if unit_dyn is None:
        return iface_inner
    return unit_dyn.dynamic_child if unit_dyn.dynamic_child is not None else unit_dyn


# ============================================================================
# Tree navigation
# ============================================================================

def expand_config_tokens(tokens: list[str]) -> tuple[list[str] | None, str | None]:
    """Expand abbreviated static-keyword tokens in a JunOS config path.

    Walks CONFIG_TREE token by token.  Static keyword children are matched
    with :func:`nos.cli.parser.resolve_prefix`; dynamic-child tokens
    (interface names, IP prefixes, etc.) and value/presence tokens are
    passed through unchanged.

    When a node has ``expand_dotted_unit=True`` (currently only the
    ``interfaces`` node) and the incoming token matches ``<name>.<digits>``,
    the token is expanded in-place:

        ens34.0  →  ens34  unit  0

    This allows ``set interfaces ens34.0 family inet address 10.0.0.1/24``
    while keeping ``set protocols isis interface ens34.0`` intact.

    Only **ambiguous** prefixes produce an error.  **Unknown** tokens (not in
    the CONFIG_TREE and no dynamic child at that level) are passed through
    verbatim along with all remaining tokens — this allows sections not yet
    modelled in the tree (e.g. ``firewall``) to reach the config store and
    validator unchanged.

    Returns ``(expanded_tokens, None)`` on success or ``(None, error_msg)``
    on ambiguous prefix.
    """
    expanded: list[str] = []
    node: Optional[ConfigNode] = CONFIG_TREE

    for i, tok in enumerate(tokens):
        if node is None or node.is_value or node.is_presence:
            # At/past a leaf: pass remaining tokens through unchanged
            expanded.extend(tokens[i:])
            return expanded, None

        if node.children:
            resolved, err = resolve_prefix(tok, list(node.children.keys()))
            if err is None:
                expanded.append(resolved)
                node = node.children[resolved]
                continue
            # Ambiguous → propagate error immediately
            if "ambiguous" in err:
                return None, err
            # Unknown static child → try dynamic child, else pass through rest
            if node.dynamic_child is not None:
                if node.expand_dotted_unit:
                    m = _DOTTED_UNIT_RE.match(tok)
                    if m:
                        expanded.extend([m.group(1), "unit", m.group(2)])
                        node = _advance_past_unit(node.dynamic_child, m.group(2))
                        continue
                expanded.append(tok)
                node = node.dynamic_child
                continue
            expanded.extend(tokens[i:])
            return expanded, None

        # No static children at this node
        if node.dynamic_child is not None:
            if node.expand_dotted_unit:
                m = _DOTTED_UNIT_RE.match(tok)
                if m:
                    expanded.extend([m.group(1), "unit", m.group(2)])
                    node = _advance_past_unit(node.dynamic_child, m.group(2))
                    continue
            expanded.append(tok)
            node = node.dynamic_child
        else:
            expanded.extend(tokens[i:])
            return expanded, None

    return expanded, None


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
        for part in _merge_compound_tokens(jpath):
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
        if node.children:
            resolved, err = resolve_prefix(token, list(node.children.keys()))
            if resolved is not None:
                walked.append(resolved)
                node = node.children[resolved]
            elif node.dynamic_child is not None:
                if node.expand_dotted_unit:
                    m = _DOTTED_UNIT_RE.match(token)
                    if m:
                        walked.extend([m.group(1), "unit", m.group(2)])
                        node = _advance_past_unit(node.dynamic_child, m.group(2))
                        continue
                walked.append(token)
                node = node.dynamic_child
            else:
                return []
        elif node.dynamic_child is not None:
            if node.expand_dotted_unit:
                m = _DOTTED_UNIT_RE.match(token)
                if m:
                    walked.extend([m.group(1), "unit", m.group(2)])
                    node = _advance_past_unit(node.dynamic_child, m.group(2))
                    continue
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

# JunOS-style option specs for ping / traceroute completion.
# Value: (value_hint_or_None, description).  None means a presence flag.
_PING_OPTS: dict[str, tuple[Optional[str], str]] = {
    "count":            ("<1-255>",       "Number of ICMP echo requests"),
    "do-not-fragment":  (None,             "Set Do Not Fragment bit"),
    "interval":         ("<seconds>",      "Interval between packets"),
    "no-resolve":       (None,             "Do not resolve hostnames"),
    "routing-instance": ("<name>",         "Routing instance (Phase 2, ignored)"),
    "size":             ("<bytes>",        "Packet size in bytes"),
    "source":           ("<ip-address>",   "Source IP address"),
    "ttl":              ("<1-255>",        "IP Time To Live"),
}

_TRACEROUTE_OPTS: dict[str, tuple[Optional[str], str]] = {
    "as-number-lookup": (None,             "Show AS numbers (Phase 2, ignored)"),
    "no-resolve":       (None,             "Do not resolve hostnames"),
    "source":           ("<ip-address>",   "Source IP address"),
    "ttl":              ("<1-255>",        "Maximum TTL / hop count"),
    "wait":             ("<seconds>",      "Probe timeout in seconds"),
}

_SHOW_OPER_ARGS = {
    "arp": "Show ARP table",
    "ipv6": "Show IPv6 information",
    "interfaces": "Show interface status and counters",
    "ethernet-switching": "Show Ethernet switching table (bridge FDB / MAC table)",
    "route": "Show routing table",
    "bgp": "Show BGP information",
    "isis": "Show IS-IS information",
    "vlans": "Show VLAN table",
    "system": "Show system information",
    "forwarding": "Show PFE forwarding mode",
    "configuration": "Show running configuration (tree format)",
}

_ARP_SUBCMDS: dict[str, str] = {
    "interface": "Filter by interface name",
    "hostname":  "Filter by IP address",
}

_ETH_SWITCH_TABLE_SUBCMDS: dict[str, str] = {
    "interface": "Filter by interface name",
    "vlan":      "Filter by VLAN name or ID",
    "summary":   "Show per-VLAN and per-interface entry counts",
}

_ETH_SWITCH_MAIN_SUBCMDS: dict[str, str] = {
    "table":      "Show MAC/FDB table",
    "interface":  "Show per-interface switching information",
    "statistics": "Show per-interface switching statistics",
    "flood":      "Show flood group membership",
}

_OPER_PIPE_VERBS: dict[str, str] = {
    "display": "Change output format",
    "match":   "Show lines matching a pattern",
    "except":  "Show lines not matching a pattern",
    "find":    "Show lines starting from first match",
    "count":   "Count output lines",
    "no-more": "Disable pagination",
}

_CONFIGURE_PIPE_VERBS: dict[str, str] = {
    "compare": "Compare candidate with running config",
    "display": "Change output format",
    "match":   "Show lines matching a pattern",
    "except":  "Show lines not matching a pattern",
    "find":    "Show lines starting from first match",
    "count":   "Count output lines",
    "no-more": "Disable pagination",
}

_IFACE_SUB_CMDS = {
    "terse": "One-line interface status",
    "description": "Interface descriptions",
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


def _complete_probe_opts(
    opts: dict[str, tuple[Optional[str], str]],
    walk_tokens: list[str],
    prefix: str,
) -> Generator[Completion, None, None]:
    """Yield completions for JunOS ping/traceroute options.

    *walk_tokens* are the fully-typed tokens that follow the target host.
    *prefix* is the partial token currently being typed (empty when the
    cursor is at a fresh word boundary).
    """
    used: set[str] = set()
    pending_value_for: Optional[str] = None

    for tok in walk_tokens:
        if pending_value_for is not None:
            used.add(pending_value_for)
            pending_value_for = None
        elif tok in opts:
            hint, _ = opts[tok]
            if hint is not None:
                pending_value_for = tok
            else:
                used.add(tok)

    if pending_value_for is not None:
        hint, desc = opts[pending_value_for]
        yield Completion(hint, -len(prefix), display_meta=desc)
        return

    for kw, (_, desc) in sorted(opts.items()):
        if kw in used:
            continue
        if kw.startswith(prefix):
            yield Completion(kw, -len(prefix), display_meta=desc)


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
        resolved, err = resolve_prefix(cmd, list(_OPERATIONAL_CMDS.keys()))
        if err:
            return
        if resolved == "show":
            yield from self._complete_show_operational(rest, completing_new)
        elif resolved == "ping":
            yield from self._complete_ping_options(rest, completing_new)
        elif resolved == "traceroute":
            yield from self._complete_traceroute_options(rest, completing_new)

    def _complete_pipe_verbs(
        self,
        after_pipe: list[str],
        completing_new: bool,
        pipe_verbs: dict[str, str],
    ) -> Generator[Completion, None, None]:
        """Complete pipe verb or 'display set' for the segment after the last '|'."""
        pipe_prefix = "" if completing_new else (after_pipe[-1] if after_pipe else "")

        if not after_pipe or (len(after_pipe) == 1 and not completing_new):
            for verb, desc in pipe_verbs.items():
                if verb.startswith(pipe_prefix):
                    yield Completion(verb, -len(pipe_prefix), display_meta=desc)
        elif after_pipe:
            resolved_verb, _ = resolve_prefix(
                after_pipe[0].lower(), list(pipe_verbs.keys())
            )
            if resolved_verb == "display":
                sub_prefix = (
                    "" if completing_new else (after_pipe[1] if len(after_pipe) > 1 else "")
                )
                if not after_pipe[1:] or (len(after_pipe) == 2 and not completing_new):
                    if "set".startswith(sub_prefix):
                        yield Completion("set", -len(sub_prefix),
                                         display_meta="Set commands format")
            # Offer "|" for chaining when the current segment is complete:
            # no-arg verbs are done after the verb itself; one-arg verbs need verb + arg.
            if completing_new:
                _NO_ARG_VERBS = {"count", "no-more", "compare"}
                if resolved_verb in _NO_ARG_VERBS or len(after_pipe) >= 2:
                    yield Completion("|", display_meta="Chain another filter")

    def _complete_show_operational(
        self, rest: list[str], completing_new: bool
    ) -> Generator[Completion, None, None]:
        # Pipe handling: if "|" appears anywhere, complete after the last "|".
        # This covers both single-pipe and chained-pipe for all show sub-commands.
        if "|" in rest:
            last_pipe_idx = max(i for i, t in enumerate(rest) if t == "|")
            yield from self._complete_pipe_verbs(
                rest[last_pipe_idx + 1:], completing_new, _OPER_PIPE_VERBS
            )
            return

        prefix = "" if completing_new else (rest[-1] if rest else "")

        if not rest or (len(rest) == 1 and not completing_new):
            # First token after "show": operational sub-commands
            for kw, desc in _SHOW_OPER_ARGS.items():
                if kw.startswith(prefix):
                    yield Completion(kw, -len(prefix), display_meta=desc)
            if "|".startswith(prefix):
                yield Completion("|", -len(prefix), display_meta="Filter output")
            return

        resolved_sub, _ = resolve_prefix(rest[0].lower(), list(_SHOW_OPER_ARGS.keys()))

        # "show arp [interface <if>|hostname <ip>]"
        if resolved_sub == "arp":
            arp_rest = rest[1:]
            arp_prefix = "" if completing_new else (arp_rest[-1] if arp_rest else "")
            if not arp_rest or (len(arp_rest) == 1 and not completing_new):
                for kw, meta in _ARP_SUBCMDS.items():
                    if kw.startswith(arp_prefix):
                        yield Completion(kw, -len(arp_prefix), display_meta=meta)
            elif completing_new:
                last_kw = arp_rest[-1].lower()
                if last_kw == "interface":
                    yield Completion("<interface-name>", display_meta="Interface name")
                elif last_kw == "hostname":
                    yield Completion("<ip-address>", display_meta="IP address")
            if completing_new:
                yield Completion("|", display_meta="Filter output")
            return

        # "show ipv6 neighbors [interface <if>]"
        if resolved_sub == "ipv6":
            ipv6_rest = rest[1:]
            ipv6_prefix = "" if completing_new else (ipv6_rest[-1] if ipv6_rest else "")
            if not ipv6_rest or (len(ipv6_rest) == 1 and not completing_new):
                if "neighbors".startswith(ipv6_prefix):
                    yield Completion(
                        "neighbors", -len(ipv6_prefix),
                        display_meta="Show IPv6 neighbor table",
                    )
            elif ipv6_rest[0].lower() == "neighbors":
                nbr_rest = ipv6_rest[1:]
                nbr_prefix = "" if completing_new else (nbr_rest[-1] if nbr_rest else "")
                if not nbr_rest or (len(nbr_rest) == 1 and not completing_new):
                    if "interface".startswith(nbr_prefix):
                        yield Completion(
                            "interface", -len(nbr_prefix),
                            display_meta="Filter by interface name",
                        )
                elif completing_new and nbr_rest[-1].lower() == "interface":
                    yield Completion("<interface-name>", display_meta="Interface name")
            if completing_new:
                yield Completion("|", display_meta="Filter output")
            return

        # "show interfaces [terse|description]"
        if resolved_sub == "interfaces":
            sub_rest = rest[1:]
            sub_prefix = "" if completing_new else (sub_rest[-1] if sub_rest else "")
            if not sub_rest or (len(sub_rest) == 1 and not completing_new):
                for kw, meta in _IFACE_SUB_CMDS.items():
                    if kw.startswith(sub_prefix):
                        yield Completion(kw, -len(sub_prefix), display_meta=meta)
            if completing_new:
                yield Completion("|", display_meta="Filter output")
            return

        # "show ethernet-switching [table|interface|statistics|flood] [...]"
        if resolved_sub == "ethernet-switching":
            eth_rest = rest[1:]
            eth_prefix = "" if completing_new else (eth_rest[-1] if eth_rest else "")
            if not eth_rest or (len(eth_rest) == 1 and not completing_new):
                # Offer main subcommands: table, interface, statistics, flood
                for kw, meta in _ETH_SWITCH_MAIN_SUBCMDS.items():
                    if kw.startswith(eth_prefix):
                        yield Completion(kw, -len(eth_prefix), display_meta=meta)
            elif eth_rest[0].lower() == "table":
                tbl_rest = eth_rest[1:]
                tbl_prefix = "" if completing_new else (tbl_rest[-1] if tbl_rest else "")
                if not tbl_rest or (len(tbl_rest) == 1 and not completing_new):
                    for kw, meta in _ETH_SWITCH_TABLE_SUBCMDS.items():
                        if kw.startswith(tbl_prefix):
                            yield Completion(kw, -len(tbl_prefix), display_meta=meta)
                elif len(tbl_rest) >= 1:
                    last_kw = tbl_rest[-2] if len(tbl_rest) >= 2 else tbl_rest[0]
                    # After "interface" or "vlan" offer a value hint
                    if not completing_new and len(tbl_rest) == 1:
                        pass  # still typing the keyword itself — handled above
                    elif completing_new and last_kw.lower() in ("interface", "vlan"):
                        hint = "<interface-name>" if last_kw.lower() == "interface" else "<vlan-name-or-id>"
                        yield Completion(hint, display_meta=_ETH_SWITCH_TABLE_SUBCMDS[last_kw.lower()])
            elif eth_rest[0].lower() in ("interface", "statistics"):
                # For "interface" and "statistics", offer optional interface name argument
                iface_rest = eth_rest[1:]
                iface_prefix = "" if completing_new else (iface_rest[-1] if iface_rest else "")
                if not iface_rest or (len(iface_rest) == 1 and not completing_new):
                    yield Completion(
                        "<interface-name>", -len(iface_prefix),
                        display_meta="Optional: filter by interface name"
                    )
            if completing_new:
                yield Completion("|", display_meta="Filter output")
            return

        # "show configuration [<section-path>] [| <pipe-verb> ...]"
        if resolved_sub == "configuration":
            config_rest = rest[1:]
            yield from complete_config_tokens(config_rest, completing_new, [], self.store)
            if completing_new:
                yield Completion("|", display_meta="Filter output")
            return

        # All other show sub-commands (route, bgp, isis, vlans, system, forwarding)
        if completing_new:
            yield Completion("|", display_meta="Filter output")

    def _complete_ping_options(
        self, rest: list[str], completing_new: bool
    ) -> Generator[Completion, None, None]:
        if not rest or (len(rest) == 1 and not completing_new):
            prefix = rest[0] if rest else ""
            yield Completion("<host>", -len(prefix), display_meta="Hostname or IP address")
            return
        opt_tokens = rest[1:]
        prefix = "" if completing_new else (opt_tokens[-1] if opt_tokens else "")
        walk = opt_tokens if completing_new else opt_tokens[:-1]
        yield from _complete_probe_opts(_PING_OPTS, walk, prefix)

    def _complete_traceroute_options(
        self, rest: list[str], completing_new: bool
    ) -> Generator[Completion, None, None]:
        if not rest or (len(rest) == 1 and not completing_new):
            prefix = rest[0] if rest else ""
            yield Completion("<host>", -len(prefix), display_meta="Hostname or IP address")
            return
        opt_tokens = rest[1:]
        prefix = "" if completing_new else (opt_tokens[-1] if opt_tokens else "")
        walk = opt_tokens if completing_new else opt_tokens[:-1]
        yield from _complete_probe_opts(_TRACEROUTE_OPTS, walk, prefix)

    # ------------------------------------------------------------------
    # Configure mode
    # ------------------------------------------------------------------

    def _complete_configure(
        self, cmd: str, rest: list[str], completing_new: bool
    ) -> Generator[Completion, None, None]:
        resolved, err = resolve_prefix(cmd, list(_CONFIGURE_CMDS.keys()))
        if err:
            return
        if resolved in ("set", "delete", "edit"):
            yield from complete_config_tokens(
                rest, completing_new, self.edit_path, self.store
            )
        elif resolved == "commit":
            yield from self._complete_commit(rest, completing_new)
        elif resolved == "rollback":
            yield from self._complete_rollback(rest, completing_new)
        elif resolved == "run":
            # Delegate to operational completions
            if rest:
                sub_cmd = rest[0].lower()
                yield from self._complete_operational(sub_cmd, rest[1:], completing_new)
            else:
                for kw, desc in _OPERATIONAL_CMDS.items():
                    if kw != "configure":
                        yield Completion(kw, display_meta=desc)
        elif resolved == "show":
            yield from self._complete_show_configure(rest, completing_new)
        elif resolved == "up":
            if not rest or (len(rest) == 1 and not completing_new):
                prefix = rest[0] if rest and not completing_new else ""
                yield Completion("<count>", -len(prefix), display_meta="Number of levels to go up")

    def _complete_show_configure(
        self, rest: list[str], completing_new: bool
    ) -> Generator[Completion, None, None]:
        if "|" in rest:
            last_pipe_idx = max(i for i, t in enumerate(rest) if t == "|")
            yield from self._complete_pipe_verbs(
                rest[last_pipe_idx + 1:], completing_new, _CONFIGURE_PIPE_VERBS
            )
            return

        # Config section path completions, relative to the current edit_path.
        yield from complete_config_tokens(
            rest, completing_new, self.edit_path, self.store
        )

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
