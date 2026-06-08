"""Configure mode handler for NOS CLI.

Handles all commands available at the configure prompt (#): set, delete,
edit, up, top, show, commit, rollback, discard, run, exit.

The configure mode maintains an *edit_path* that represents the current
position in the config hierarchy (JunOS-format, hyphenated).
"""
from __future__ import annotations

from typing import Any, Optional

from nos.cli.completer import expand_config_tokens
from nos.cli.parser import CLIMode, CommandParser, CommandType, ParseResult
from nos.config.commit import CommitEngine, CommitError, RollbackError
from nos.config.diff import diff
from nos.config.serializer import (
    _INLINE_SIBLING_PAIRS,
    _merge_compound_tokens,
    from_set_commands,
)
from nos.config.store import ConfigStore

_parser = CommandParser()


# ============================================================================
# Config rendering
# ============================================================================

_COMPOUND_KEY_DISPLAY: dict[str, str] = {
    "family_inet":              "family inet",
    "family_inet6":             "family inet6",
    "family_ethernet_switching": "family ethernet-switching",
}


def _j2k(key: str) -> str:
    """snake_case internal key → JunOS display form (two words for compound keys)."""
    return _COMPOUND_KEY_DISPLAY.get(str(key)) or str(key).replace("_", "-")


def render_block(cfg: Any, depth: int = 0) -> str:
    """Render *cfg* as a JunOS hierarchical block (for 'show' in configure mode)."""
    pad = "    " * depth
    if cfg is None or cfg is False:
        return ""
    if cfg is True:
        return ""  # caller emits bare key
    if isinstance(cfg, (int, float)):
        return str(cfg)
    if isinstance(cfg, str):
        return f'"{cfg}"' if (" " in cfg or not cfg) else cfg
    if isinstance(cfg, list):
        return "\n".join(render_block(item, depth) for item in cfg)

    if not isinstance(cfg, dict):
        return str(cfg)

    lines: list[str] = []
    for raw_key, val in sorted(cfg.items(), key=lambda x: str(x[0])):
        key = _j2k(str(raw_key))
        if val is None or val is False:
            continue
        if val is True:
            lines.append(f"{pad}{key};")
        elif isinstance(val, dict):
            if not val:
                lines.append(f"{pad}{key};")
            else:
                inner = render_block(val, depth + 1)
                lines.append(f"{pad}{key} {{")
                if inner:
                    lines.append(inner)
                lines.append(f"{pad}}}")
        elif isinstance(val, (int, float)):
            lines.append(f"{pad}{key} {val};")
        elif isinstance(val, str):
            quoted = f'"{val}"' if (" " in val or not val) else val
            lines.append(f"{pad}{key} {quoted};")
        elif isinstance(val, list):
            for item in val:
                if isinstance(item, str):
                    quoted = f'"{item}"' if (" " in item or not item) else item
                    lines.append(f"{pad}{key} {quoted};")
                else:
                    lines.append(f"{pad}{key} {item};")
    return "\n".join(lines)


def _get_at_path(cfg: dict, path: list[str]) -> Any:
    """Navigate *cfg* dict following *path* (internal snake_case keys)."""
    node: Any = cfg
    for part in _merge_compound_tokens(path):
        internal = part.replace("-", "_")
        if isinstance(node, dict) and internal in node:
            node = node[internal]
        else:
            return None
    return node


def _deep_merge(base: dict, overlay: dict) -> None:
    """Merge *overlay* into *base* in place, recursively."""
    for k, v in overlay.items():
        existing = base.get(k)
        if isinstance(existing, dict) and isinstance(v, dict):
            _deep_merge(existing, v)
        elif not isinstance(existing, dict) and isinstance(v, dict):
            # Presence True upgraded to dict by adding a child
            base[k] = v
        else:
            base[k] = v


def _find_value_split(tokens: list[str]) -> int:
    """Return the index of the first value token in *tokens*.

    Navigates the JunOS config tree from the root.  When a node with
    ``is_value=True`` is reached, the token at the current index is the
    value string (not a further path component).  Returns ``len(tokens)``
    when all tokens are path components (presence flags or unknown paths).
    """
    from nos.cli.completer import CONFIG_TREE  # avoid circular import at module load
    node = CONFIG_TREE
    for i, tok in enumerate(tokens):
        if node.is_value:
            return i
        if tok in node.children:
            node = node.children[tok]
        elif node.dynamic_child is not None:
            node = node.dynamic_child
        else:
            break  # unknown path component – treat the rest as path
    return len(tokens)


def _quote_value(s: str) -> str:
    """Wrap *s* in double-quotes, escaping any embedded backslashes or quotes."""
    escaped = s.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def _is_int(s: str) -> bool:
    try:
        int(s)
        return True
    except ValueError:
        return False


def _maybe_hash_user_password(tokens: list[str]) -> list[str]:
    """Hash plaintext when tokens are: system login user <name> authentication password <pw>.

    The hash replaces the plaintext at index 6 so the candidate config never
    stores a cleartext password.
    """
    if (len(tokens) == 7
            and tokens[0] == "system"
            and tokens[1] == "login"
            and tokens[2] == "user"
            and tokens[4] == "authentication"
            and tokens[5] == "password"):
        from nos.drivers.kernel.users import _hash_password
        result = list(tokens)
        result[6] = _hash_password(result[6])
        return result
    return tokens


# ============================================================================
# Configure mode
# ============================================================================

class ConfigureMode:
    """Execute commands in configure mode.

    The caller is responsible for querying and updating ``edit_path`` via the
    ``edit_path`` attribute after each call.
    """

    def __init__(self, store: ConfigStore, commit_engine: CommitEngine) -> None:
        self.store = store
        self.commit_engine = commit_engine
        # Current position in config hierarchy (JunOS hyphen format)
        self.edit_path: list[str] = []

    def execute(self, line: str) -> Optional[str]:
        """Parse and execute one command line.

        Returns rendered output (may be empty string on success), or a
        message string.  Raises SystemExit to exit configure mode (caller
        catches and returns to operational mode).
        """
        result = _parser.parse(line, CLIMode.CONFIGURE)
        if result.is_error:
            return f"error: {result.error}"
        return self._dispatch(result)

    def _dispatch(self, result: ParseResult) -> Optional[str]:
        match result.command:
            case CommandType.SET:
                return self._handle_set(result.args)
            case CommandType.DELETE:
                return self._handle_delete(result.args)
            case CommandType.EDIT:
                return self._handle_edit(result.args)
            case CommandType.UP:
                return self._handle_up(int(result.args[0]))
            case CommandType.TOP:
                self.edit_path = []
                return ""
            case CommandType.SHOW:
                return self._handle_show(result.args, result.pipe)
            case CommandType.COMMIT:
                return self._handle_commit()
            case CommandType.COMMIT_AND_QUIT:
                return self._handle_commit_and_quit()
            case CommandType.COMMIT_CHECK:
                return self._handle_commit_check()
            case CommandType.COMMIT_CONFIRMED:
                return self._handle_commit_confirmed(int(result.args[0]))
            case CommandType.ROLLBACK:
                return self._handle_rollback(int(result.args[0]))
            case CommandType.DISCARD:
                return self._handle_discard()
            case CommandType.RUN:
                return self._handle_run(result.args)
            case CommandType.EXIT:
                raise SystemExit(0)  # caller interprets as "leave configure mode"
            case CommandType.UNKNOWN:
                return f"error: {result.error}"
            case _:
                return "error: command not valid in configure mode"

    # ------------------------------------------------------------------
    # set
    # ------------------------------------------------------------------

    def _handle_set(self, args: list[str]) -> str:
        if not args:
            return "error: set requires arguments"

        full_args_raw = self.edit_path + args
        full_args, exp_err = expand_config_tokens(full_args_raw)
        if exp_err:
            return f"error: {exp_err}"

        full_args = _maybe_hash_user_password(full_args)

        # Detect inline sibling pairs (e.g. "source 10.0.0.2/32 translated 172.18.4.44")
        # BEFORE quoting, so that sibling keywords like "translated" are never
        # wrapped in double-quotes.  If detected, emit one set command per pair.
        sibling_idx: Optional[int] = None
        for k in range(len(full_args) - 2):
            if (full_args[k], full_args[k + 2]) in _INLINE_SIBLING_PAIRS:
                sibling_idx = k
                break

        if sibling_idx is not None:
            prefix_toks = full_args[:sibling_idx]
            sibling_toks = full_args[sibling_idx:]
            cmds: list[str] = []
            j = 0
            while j + 1 < len(sibling_toks):
                key = sibling_toks[j]
                val = sibling_toks[j + 1]
                val_tok = val if _is_int(val) else _quote_value(val)
                cmds.append("set " + " ".join(prefix_toks + [key, val_tok]))
                j += 2
            partial = from_set_commands(cmds)
            if not partial:
                return "error: invalid set arguments"
            _deep_merge(self.store.candidate, partial)
            return ""

        # Use the config tree to find where the value begins.
        # Tokens *before* split_at are path components (left as-is so that
        # the serialiser handles hyphen→underscore conversion and dynamic keys
        # such as IP prefixes).  Tokens *at or after* split_at are the value;
        # plain strings must be double-quoted so from_set_commands treats them
        # as string scalars rather than presence flags.
        # Integers are never quoted: from_set_commands detects them and stores
        # them as int, which is what schema fields like vlan-id and AS numbers
        # expect.
        split_at = _find_value_split(full_args)

        parts: list[str] = []
        for i, tok in enumerate(full_args):
            if i >= split_at and not _is_int(tok):
                tok = _quote_value(tok)
            parts.append(tok)

        cmd = "set " + " ".join(parts)
        partial = from_set_commands([cmd])
        if not partial:
            return "error: invalid set arguments"

        # When all tokens are path components (pure presence path), check if
        # the final config-tree node is a container dict (_n node, not a
        # presence flag or value leaf).  If so, store {} so that the value
        # round-trips correctly through the schema validator — e.g. an IP
        # address key under "address" should map to {} (InetAddress defaults),
        # not True.
        if split_at == len(full_args):
            from nos.cli.completer import CONFIG_TREE, navigate_tree
            final_node = navigate_tree(CONFIG_TREE, full_args)
            if (final_node is not None
                    and not final_node.is_presence
                    and not final_node.is_value):
                leaf_path = [tok.replace("-", "_") for tok in _merge_compound_tokens(full_args)]
                node: Any = partial
                for key in leaf_path[:-1]:
                    if not isinstance(node, dict):
                        node = None
                        break
                    node = node.get(key)
                if isinstance(node, dict):
                    last_key = leaf_path[-1]
                    if node.get(last_key) is True:
                        node[last_key] = {}

        _deep_merge(self.store.candidate, partial)
        return ""

    # ------------------------------------------------------------------
    # delete
    # ------------------------------------------------------------------

    def _handle_delete(self, args: list[str]) -> str:
        if not args:
            return "error: delete requires a path"
        expanded, exp_err = expand_config_tokens(self.edit_path + args)
        if exp_err:
            return f"error: {exp_err}"
        merged = _merge_compound_tokens(expanded)
        full_path = [p.replace("-", "_") for p in merged]
        self.store.delete_candidate(full_path)
        return ""

    # ------------------------------------------------------------------
    # edit / up / top
    # ------------------------------------------------------------------

    def _handle_edit(self, args: list[str]) -> str:
        if not args:
            return "error: edit requires a path"
        expanded, exp_err = expand_config_tokens(self.edit_path + args)
        if exp_err:
            return f"error: {exp_err}"
        from nos.cli.completer import navigate_tree, CONFIG_TREE
        node = navigate_tree(CONFIG_TREE, expanded)
        if node is None:
            return f"error: {' '.join(expanded)!r} is not a valid configuration path"
        self.edit_path = expanded
        return ""

    def _handle_up(self, count: int) -> str:
        levels = min(count, len(self.edit_path))
        self.edit_path = self.edit_path[:-levels] if levels else self.edit_path
        return ""

    # ------------------------------------------------------------------
    # show
    # ------------------------------------------------------------------

    def _handle_show(self, args: list[str], pipe: Optional[str]) -> str:
        # "show | compare" → diff only
        if pipe and pipe.strip().startswith("compare"):
            return self._show_compare()

        candidate = self.store.get_candidate()
        full = self.edit_path + args
        if full:
            display_path, exp_err = expand_config_tokens(full)
            if exp_err:
                return f"error: {exp_err}"
        else:
            display_path = full
        subtree = _get_at_path(candidate, display_path)
        if subtree is None:
            if display_path:
                section = " ".join(display_path)
                return f"(no configuration for '{section}')"
            return "(empty)"

        output = render_block(subtree, depth=0)
        if pipe:
            from nos.cli.modes.operational import _apply_pipe
            cfg_for_set: Any = subtree
            for tok in reversed(display_path):
                cfg_for_set = {tok.replace("-", "_"): cfg_for_set}
            output = _apply_pipe(output, pipe, cfg_for_set)
        return output or "(empty)"

    def _show_compare(self) -> str:
        running = self.store.get_running()
        candidate = self.store.get_candidate()
        result = diff(running, candidate)
        return result if result else "No changes."

    # ------------------------------------------------------------------
    # commit
    # ------------------------------------------------------------------

    def _handle_commit(self) -> str:
        try:
            self.commit_engine.commit()
            return "commit complete"
        except CommitError as exc:
            lines = ["commit validation failed:"]
            for err in exc.errors:
                lines.append(f"  {err}")
            return "\n".join(lines)
        except Exception as exc:
            return f"commit error: {exc}"

    def _handle_commit_and_quit(self) -> str:
        try:
            self.commit_engine.commit()
            raise SystemExit(0)
        except CommitError as exc:
            lines = ["commit validation failed:"]
            for err in exc.errors:
                lines.append(f"  {err}")
            return "\n".join(lines)
        except SystemExit:
            raise
        except Exception as exc:
            return f"commit error: {exc}"

    def _handle_commit_check(self) -> str:
        result = self.commit_engine.commit_check()
        if result.is_valid:
            return "configuration check succeeds"
        lines = ["commit check — validation errors:"]
        for err in result.errors:
            lines.append(f"  {err}")
        return "\n".join(lines)

    def _handle_commit_confirmed(self, minutes: int) -> str:
        try:
            self.commit_engine.commit_confirmed(minutes)
            return (
                f"commit confirmed — will rollback in {minutes} minute(s)\n"
                "commit complete\n"
                f"Use 'commit' to confirm before the timer expires."
            )
        except CommitError as exc:
            lines = ["commit validation failed:"]
            for err in exc.errors:
                lines.append(f"  {err}")
            return "\n".join(lines)
        except Exception as exc:
            return f"commit error: {exc}"

    # ------------------------------------------------------------------
    # rollback / discard
    # ------------------------------------------------------------------

    def _handle_rollback(self, n: int) -> str:
        try:
            self.commit_engine.rollback(n)
            return f"load complete — rolled back to checkpoint {n}"
        except RollbackError as exc:
            return f"error: {exc}"
        except Exception as exc:
            return f"error: {exc}"

    def _handle_discard(self) -> str:
        self.store.discard()
        return "changes discarded"

    # ------------------------------------------------------------------
    # run
    # ------------------------------------------------------------------

    def _handle_run(self, args: list[str]) -> str:
        if not args:
            return "error: run requires an operational command"
        from nos.cli.modes.operational import OperationalMode
        oper = OperationalMode(self.store)
        line = " ".join(args)
        result = oper.execute(line)
        if result is None:
            return "error: 'configure' cannot be run from configure mode"
        return result
