from __future__ import annotations

from typing import Any


# ---------------------------------------------------------------------------
# Key conversion helpers
# ---------------------------------------------------------------------------

def _k2j(key: str) -> str:
    """Internal snake_case key → JunOS hyphen-case."""
    return str(key).replace("_", "-")


def _j2k(token: str) -> str:
    """JunOS hyphen-case token → internal snake_case key."""
    return token.replace("-", "_")


# ---------------------------------------------------------------------------
# to_set_commands
# ---------------------------------------------------------------------------

def _quote(s: str) -> str:
    escaped = s.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def _flatten(node: Any, path: list[str], out: list[str]) -> None:
    if node is None or node is False:
        return
    if node is True:
        out.append("set " + " ".join(path))
        return
    if isinstance(node, dict):
        if not node:
            # truly empty dict → presence marker (e.g. InetAddress with all defaults)
            if path:
                out.append("set " + " ".join(path))
            return
        for key, value in node.items():
            if value is None or value is False:
                continue
            _flatten(value, path + [_k2j(str(key))], out)
        return
    if isinstance(node, list):
        for item in node:
            _flatten(item, path, out)
        return
    # scalar (int, str, float, enum value as str)
    if isinstance(node, str):
        out.append("set " + " ".join(path) + " " + _quote(node))
    else:
        out.append("set " + " ".join(path) + " " + str(node))


def to_set_commands(config: dict) -> list[str]:
    """Convert an internal JSON config dict to a sorted list of JunOS set commands.

    String values are always double-quoted so from_set_commands can round-trip them.
    Boolean True / empty-dict fields are emitted as bare paths (no value).
    Boolean False and None fields are omitted.
    """
    out: list[str] = []
    _flatten(config, [], out)
    return sorted(out)


# ---------------------------------------------------------------------------
# from_set_commands — tokenizer
# ---------------------------------------------------------------------------

def _tokenize(s: str) -> list[str]:
    """Split a set-command string into tokens, honouring double-quoted strings.

    Quoted tokens are returned WITH their outer double-quotes so the caller
    can distinguish them from unquoted tokens.  Escape sequences inside
    quoted tokens are resolved (\\\" → \", \\\\ → \\).
    """
    tokens: list[str] = []
    current: list[str] = []
    in_quotes = False
    i = 0
    while i < len(s):
        c = s[i]
        if in_quotes:
            if c == "\\" and i + 1 < len(s):
                nxt = s[i + 1]
                if nxt == '"':
                    current.append('"')
                elif nxt == "\\":
                    current.append("\\")
                else:
                    current.append(c)
                    current.append(nxt)
                i += 2
                continue
            if c == '"':
                # closing quote — flush the quoted token WITH outer quotes
                tokens.append('"' + "".join(current) + '"')
                current = []
                in_quotes = False
                i += 1
                continue
            current.append(c)
        else:
            if c == '"':
                in_quotes = True
                current = []
                i += 1
                continue
            if c == " ":
                if current:
                    tokens.append("".join(current))
                    current = []
            else:
                current.append(c)
        i += 1
    if current:
        tokens.append("".join(current))
    return tokens


def _is_integer(token: str) -> bool:
    try:
        int(token)
        return True
    except ValueError:
        return False


def _is_quoted(token: str) -> bool:
    return len(token) >= 2 and token[0] == '"' and token[-1] == '"'


# ---------------------------------------------------------------------------
# from_set_commands — dict builder
# ---------------------------------------------------------------------------

def _insert(config: dict, path_keys: list[str], value: Any) -> None:
    node = config
    for key in path_keys[:-1]:
        k = _j2k(key)
        existing = node.get(k)
        if not isinstance(existing, dict):
            node[k] = {}
        node = node[k]
    last = _j2k(path_keys[-1])
    existing = node.get(last)
    if existing is None:
        node[last] = value
    elif isinstance(existing, list):
        existing.append(value)
    elif isinstance(existing, dict) and isinstance(value, dict):
        _deep_merge(existing, value)
    else:
        # Convert to list on repeated scalar key; last-write-wins if same value
        if existing == value:
            pass
        elif not isinstance(existing, dict) and not isinstance(value, dict):
            node[last] = [existing, value]
        else:
            node[last] = value


def _deep_merge(base: dict, overlay: dict) -> None:
    for k, v in overlay.items():
        if k in base and isinstance(base[k], dict) and isinstance(v, dict):
            _deep_merge(base[k], v)
        else:
            base[k] = v


def from_set_commands(commands: list[str]) -> dict:
    """Parse a list of JunOS set commands into an internal config dict.

    Parsing rules (designed to round-trip output from to_set_commands):

    - Quoted last token  → string scalar value (outer quotes stripped)
    - Integer last token → int scalar value
    - All other tokens   → path components; leaf is set to True (presence flag)

    Keys are converted from hyphen-case to snake_case.
    Repeated scalar values at the same path are accumulated into a list.
    """
    config: dict = {}
    for raw in commands:
        raw = raw.strip()
        if not raw.startswith("set "):
            continue
        tokens = _tokenize(raw[4:])
        if not tokens:
            continue

        last = tokens[-1]
        if _is_quoted(last):
            # quoted string — strip outer quotes, already unescaped by tokenizer
            path_keys = tokens[:-1]
            value: Any = last[1:-1]
        elif _is_integer(last):
            path_keys = tokens[:-1]
            value = int(last)
        else:
            # unquoted non-integer → path component / presence flag
            path_keys = tokens
            value = True

        if not path_keys:
            continue

        _insert(config, path_keys, value)

    return config
