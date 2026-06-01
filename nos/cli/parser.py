"""JunOS-style command parser for NOS CLI.

Parses raw input lines into structured ParseResult objects for both
operational mode (> prompt) and configure mode (# prompt).
"""
from __future__ import annotations

import shlex
from dataclasses import dataclass, field
from enum import Enum, auto
from typing import Optional


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

        # Split pipe filter before tokenising
        pipe_text: Optional[str] = None
        if " | " in line:
            main, _, rest = line.partition(" | ")
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
        if cmd == "show":
            return ParseResult(command=CommandType.SHOW, args=args, pipe=pipe, raw=raw)
        if cmd == "ping":
            return ParseResult(command=CommandType.PING, args=args, raw=raw)
        if cmd in ("traceroute", "tracert"):
            return ParseResult(command=CommandType.TRACEROUTE, args=args, raw=raw)
        if cmd == "configure":
            return ParseResult(command=CommandType.CONFIGURE, args=args, raw=raw)
        if cmd in ("exit", "quit"):
            return ParseResult(command=CommandType.EXIT, raw=raw)
        return ParseResult(
            command=CommandType.UNKNOWN,
            error=f"unknown command: {cmd!r}",
            raw=raw,
        )

    # ------------------------------------------------------------------
    # Configure mode
    # ------------------------------------------------------------------

    def _parse_configure(
        self, cmd: str, args: list[str], pipe: Optional[str], raw: str
    ) -> ParseResult:
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
            case "exit" | "quit":
                return ParseResult(command=CommandType.EXIT, raw=raw)
            case _:
                return ParseResult(
                    command=CommandType.UNKNOWN,
                    error=f"unknown command: {cmd!r}",
                    raw=raw,
                )

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
        sub = args[0].lower()
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
        return ParseResult(
            command=CommandType.UNKNOWN,
            error=f"commit: unknown sub-command {args[0]!r}",
            raw=raw,
        )

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
