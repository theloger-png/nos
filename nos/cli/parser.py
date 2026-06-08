"""JunOS-style command parser for NOS CLI.

Parses raw input lines into structured ParseResult objects for both
operational mode (> prompt) and configure mode (# prompt).
"""
from __future__ import annotations

import shlex
from dataclasses import dataclass, field
from enum import Enum, auto
from typing import Optional


# ============================================================================
# Prefix resolution
# ============================================================================

_OPERATIONAL_CMDS: list[str] = ["configure", "exit", "ping", "show", "traceroute"]
_CONFIGURE_CMDS: list[str] = [
    "commit", "delete", "discard", "edit", "exit",
    "rollback", "run", "set", "show", "top", "up",
]
_COMMIT_SUBCMDS: list[str] = ["and-quit", "check", "confirmed"]


def resolve_prefix(token: str, candidates: list[str]) -> tuple[str | None, str | None]:
    """Unambiguous prefix match *token* against *candidates*.

    Exact matches win unconditionally.  Otherwise all candidates whose name
    starts with *token* are collected; exactly one → success; more than one
    → ambiguous error; zero → unknown error.

    Returns ``(resolved, None)`` on success or ``(None, error_message)`` on
    failure.
    """
    if token in candidates:
        return token, None
    matches = [c for c in candidates if c.startswith(token)]
    if len(matches) == 1:
        return matches[0], None
    if len(matches) > 1:
        return None, f"ambiguous command: {token!r} (matches: {', '.join(sorted(matches))})"
    return None, f"unknown command: {token!r}"


class CLIMode(Enum):
    OPERATIONAL = auto()
    CONFIGURE = auto()


class CommandType(Enum):
    # Operational
    SHOW = "show"
    PING = "ping"
    TRACEROUTE = "traceroute"
    CONFIGURE = "configure"
    # Configure
    SET = "set"
    DELETE = "delete"
    EDIT = "edit"
    UP = "up"
    TOP = "top"
    COMMIT = "commit"
    COMMIT_AND_QUIT = "commit_and_quit"
    COMMIT_CONFIRMED = "commit_confirmed"
    COMMIT_CHECK = "commit_check"
    ROLLBACK = "rollback"
    DISCARD = "discard"
    RUN = "run"
    # Common
    EXIT = "exit"
    UNKNOWN = "unknown"


@dataclass
class ParseResult:
    """Structured result of parsing one CLI line."""

    command: CommandType
    args: list[str] = field(default_factory=list)
    # Pipe filter string, e.g. "match ge-" or "except 0.0.0.0"
    pipe: Optional[str] = None
    error: Optional[str] = None
    raw: str = ""

    @property
    def is_error(self) -> bool:
        return self.error is not None


class CommandParser:
    """Parse CLI input lines for both operational and configure modes."""

    def parse(self, line: str, mode: CLIMode) -> ParseResult:
        """Parse *line* in the given *mode* and return a ParseResult."""
        raw = line
        line = line.strip()
        if not line:
            return ParseResult(command=CommandType.UNKNOWN, raw=raw)

        # Split pipe filter before tokenising (handle | with or without spaces)
        pipe_text: Optional[str] = None
        if "|" in line:
            main, _, rest = line.partition("|")
            line = main.rstrip()
            pipe_text = rest.strip()

        try:
            tokens = shlex.split(line)
        except ValueError as exc:
            return ParseResult(command=CommandType.UNKNOWN, error=str(exc), raw=raw)

        if not tokens:
            return ParseResult(command=CommandType.UNKNOWN, raw=raw)

        cmd_word = tokens[0].lower()
        args = tokens[1:]

        if mode == CLIMode.OPERATIONAL:
            return self._parse_operational(cmd_word, args, pipe_text, raw)
        return self._parse_configure(cmd_word, args, pipe_text, raw)

    # ------------------------------------------------------------------
    # Operational mode
    # ------------------------------------------------------------------

    def _parse_operational(
        self, cmd: str, args: list[str], pipe: Optional[str], raw: str
    ) -> ParseResult:
        # Aliases resolved before prefix matching
        if cmd == "tracert":
            return ParseResult(command=CommandType.TRACEROUTE, args=args, raw=raw)
        if cmd == "quit":
            return ParseResult(command=CommandType.EXIT, raw=raw)

        resolved, err = resolve_prefix(cmd, _OPERATIONAL_CMDS)
        if err:
            return ParseResult(command=CommandType.UNKNOWN, error=err, raw=raw)
        cmd = resolved

        match cmd:
            case "show":
                return ParseResult(command=CommandType.SHOW, args=args, pipe=pipe, raw=raw)
            case "ping":
                return ParseResult(command=CommandType.PING, args=args, raw=raw)
            case "traceroute":
                return ParseResult(command=CommandType.TRACEROUTE, args=args, raw=raw)
            case "configure":
                return ParseResult(command=CommandType.CONFIGURE, args=args, raw=raw)
            case "exit":
                return ParseResult(command=CommandType.EXIT, raw=raw)
            case _:  # pragma: no cover
                return ParseResult(command=CommandType.UNKNOWN, error=f"unknown command: {cmd!r}", raw=raw)

    # ------------------------------------------------------------------
    # Configure mode
    # ------------------------------------------------------------------

    def _parse_configure(
        self, cmd: str, args: list[str], pipe: Optional[str], raw: str
    ) -> ParseResult:
        # Alias resolved before prefix matching
        if cmd == "quit":
            return ParseResult(command=CommandType.EXIT, raw=raw)

        resolved, err = resolve_prefix(cmd, _CONFIGURE_CMDS)
        if err:
            return ParseResult(command=CommandType.UNKNOWN, error=err, raw=raw)
        cmd = resolved

        match cmd:
            case "set":
                return ParseResult(command=CommandType.SET, args=args, raw=raw)
            case "delete":
                return ParseResult(command=CommandType.DELETE, args=args, raw=raw)
            case "edit":
                return ParseResult(command=CommandType.EDIT, args=args, raw=raw)
            case "up":
                return self._parse_up(args, raw)
            case "top":
                return ParseResult(command=CommandType.TOP, raw=raw)
            case "show":
                return ParseResult(command=CommandType.SHOW, args=args, pipe=pipe, raw=raw)
            case "commit":
                return self._parse_commit(args, raw)
            case "rollback":
                return self._parse_rollback(args, raw)
            case "discard":
                return ParseResult(command=CommandType.DISCARD, raw=raw)
            case "run":
                return ParseResult(command=CommandType.RUN, args=args, raw=raw)
            case "exit":
                return ParseResult(command=CommandType.EXIT, raw=raw)
            case _:  # pragma: no cover
                return ParseResult(command=CommandType.UNKNOWN, error=f"unknown command: {cmd!r}", raw=raw)

    def _parse_up(self, args: list[str], raw: str) -> ParseResult:
        count = 1
        if args:
            try:
                count = int(args[0])
                if count < 1:
                    raise ValueError
            except ValueError:
                return ParseResult(
                    command=CommandType.UNKNOWN,
                    error=f"up: expected positive integer, got {args[0]!r}",
                    raw=raw,
                )
        return ParseResult(command=CommandType.UP, args=[str(count)], raw=raw)

    def _parse_commit(self, args: list[str], raw: str) -> ParseResult:
        if not args:
            return ParseResult(command=CommandType.COMMIT, raw=raw)
        sub_raw = args[0].lower()
        resolved, err = resolve_prefix(sub_raw, _COMMIT_SUBCMDS)
        if err:
            return ParseResult(
                command=CommandType.UNKNOWN,
                error=f"commit: {err}",
                raw=raw,
            )
        sub = resolved
        if sub == "and-quit":
            return ParseResult(command=CommandType.COMMIT_AND_QUIT, raw=raw)
        if sub == "check":
            return ParseResult(command=CommandType.COMMIT_CHECK, raw=raw)
        if sub == "confirmed":
            if len(args) < 2:
                return ParseResult(
                    command=CommandType.UNKNOWN,
                    error="commit confirmed requires <minutes>",
                    raw=raw,
                )
            try:
                minutes = int(args[1])
                if minutes < 1:
                    raise ValueError
            except ValueError:
                return ParseResult(
                    command=CommandType.UNKNOWN,
                    error=f"commit confirmed: invalid minutes {args[1]!r}",
                    raw=raw,
                )
            return ParseResult(
                command=CommandType.COMMIT_CONFIRMED, args=[str(minutes)], raw=raw
            )
        return ParseResult(command=CommandType.UNKNOWN, error=f"commit: unknown sub-command {sub!r}", raw=raw)  # pragma: no cover

    def _parse_rollback(self, args: list[str], raw: str) -> ParseResult:
        if not args:
            return ParseResult(
                command=CommandType.UNKNOWN,
                error="rollback requires <0-49>",
                raw=raw,
            )
        try:
            n = int(args[0])
            if not (0 <= n <= 49):
                raise ValueError
        except ValueError:
            return ParseResult(
                command=CommandType.UNKNOWN,
                error=f"rollback: invalid index {args[0]!r} (must be 0-49)",
                raw=raw,
            )
        return ParseResult(command=CommandType.ROLLBACK, args=[str(n)], raw=raw)
