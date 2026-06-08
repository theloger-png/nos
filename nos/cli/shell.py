"""NOS CLI main shell loop.

Starts a prompt_toolkit-powered interactive session that switches between
operational mode (> prompt) and configure mode (# prompt), identical in
feel to JunOS.  Entry point: nos.cli.shell:main.
"""
from __future__ import annotations

import getpass
import logging
import socket
import sys
from pathlib import Path
from typing import Optional

_log = logging.getLogger(__name__)

from prompt_toolkit import PromptSession, print_formatted_text
from prompt_toolkit.completion import CompleteEvent
from prompt_toolkit.document import Document
from prompt_toolkit.formatted_text import ANSI, FormattedText, HTML, to_plain_text
from prompt_toolkit.history import FileHistory
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.styles import Style

from nos.cli.completer import NOSCompleter
from nos.cli.modes.configure import ConfigureMode
from nos.cli.modes.operational import OperationalMode
from nos.cli.parser import CLIMode
from nos.config.applier import ConfigApplier
from nos.config.commit import CommitEngine
from nos.config.store import ConfigStore
from nos.config.validator import ConfigValidator
from nos.drivers.dhcp.dnsmasq import DnsmasqDriver
from nos.drivers.frr import FRRClient
from nos.drivers.kernel import KernelDriver
from nos.pfe.manager import PFEManager


# ============================================================================
# Shell style
# ============================================================================

_STYLE = Style.from_dict({
    "prompt.user": "bold ansigreen",
    "prompt.sep": "bold",
    "prompt.host": "bold ansigreen",
    "prompt.context": "ansiblue",
    "prompt.mode.oper": "bold",
    "prompt.mode.conf": "bold ansired",
})


# ============================================================================
# NOS Shell
# ============================================================================

class NOSShell:
    """Interactive JunOS-like shell.

    Maintains mode state and delegates command execution to
    OperationalMode and ConfigureMode handlers.
    """

    def __init__(
        self,
        store: Optional[ConfigStore] = None,
        username: Optional[str] = None,
        hostname: Optional[str] = None,
        history_file: Optional[Path] = None,
    ) -> None:
        self.store = store or ConfigStore(base_dir="/opt/nos")
        validator = ConfigValidator()
        pfe = PFEManager()
        pfe.start()
        kernel_driver = KernelDriver()
        frr_client = FRRClient()
        dhcp_driver = DnsmasqDriver()
        applier = ConfigApplier(kernel_driver, frr_client, pfe, store=self.store, dhcp_driver=dhcp_driver)
        self.commit_engine = CommitEngine(self.store, validator=validator, applier=applier)
        self.username = username or getpass.getuser()
        self.hostname = hostname or socket.gethostname().split(".")[0]
        self._history_file = history_file or Path.home() / ".nos_history"

        self.mode = CLIMode.OPERATIONAL
        self.oper_handler = OperationalMode(self.store, pfe=pfe)
        self.conf_handler = ConfigureMode(self.store, self.commit_engine)

        _log.info("Applying running configuration on startup...")
        try:
            applier.apply({}, self.store.get_running())
        except Exception as exc:
            _log.error("Startup config apply failed: %s", exc)

    # ------------------------------------------------------------------
    # Prompt construction
    # ------------------------------------------------------------------

    def _build_prompt(self) -> list:
        """Return a prompt_toolkit FormattedText prompt."""
        user = self.username
        host = self.hostname

        if self.mode == CLIMode.OPERATIONAL:
            return [
                ("class:prompt.user", user),
                ("class:prompt.sep", "@"),
                ("class:prompt.host", host),
                ("class:prompt.mode.oper", "> "),
            ]

        # Configure mode
        path = self.conf_handler.edit_path
        parts: list[tuple[str, str]] = [
            ("class:prompt.user", user),
            ("class:prompt.sep", "@"),
            ("class:prompt.host", host),
        ]
        if path:
            ctx = " ".join(path)
            parts += [
                ("", " "),
                ("class:prompt.context", f"({ctx})"),
            ]
        parts.append(("class:prompt.mode.conf", "# "))
        return parts

    # ------------------------------------------------------------------
    # Key bindings
    # ------------------------------------------------------------------

    def _build_key_bindings(self, completer: NOSCompleter) -> KeyBindings:
        bindings = KeyBindings()

        @bindings.add("?")
        def show_help(event):
            """Display inline help for the current cursor position."""
            buf = event.current_buffer
            text = buf.document.text_before_cursor
            doc = Document(text, len(text))
            completions = list(
                completer.get_completions(doc, CompleteEvent(completion_requested=True))
            )
            # Write below current line
            output = event.app.output
            output.write("\n")
            if completions:
                output.write("Possible completions:\n")
                for c in completions:
                    kw = str(c.text)
                    meta = to_plain_text(c.display_meta) if c.display_meta else ""
                    output.write(f"  {kw:<30}  {meta}\n")
            else:
                output.write("  <no completions available>\n")
            output.write("\n")
            output.flush()
            # Redraw the prompt
            event.app.renderer.reset()

        @bindings.add(" ")
        def complete_on_space(event):
            """JunOS-style space: complete unambiguous prefix; show options when ambiguous."""
            buf = event.current_buffer

            # Active selection in the completion menu → apply it (like Tab)
            if buf.complete_state and buf.complete_state.current_completion is not None:
                buf.apply_completion(buf.complete_state.current_completion)
                return

            doc = buf.document
            completions = list(
                completer.get_completions(doc, CompleteEvent(completion_requested=True))
            )

            if len(completions) == 1 and completions[0].text:
                # Exactly one non-empty partial match → complete it, then advance
                buf.apply_completion(completions[0])
                buf.insert_text(" ")
            else:
                # No match, already-complete word, or ambiguous → insert space + show menu
                buf.insert_text(" ")
                if completions:
                    buf.start_completion(select_first=False)

        @bindings.add("c-c")
        def handle_ctrl_c(event):
            """Ctrl+C clears the current line (JunOS behaviour)."""
            event.current_buffer.reset()
            event.app.output.write("^C\n")
            event.app.output.flush()

        @bindings.add("c-x")
        def handle_ctrl_x(event):
            """Ctrl+X clears the current input line."""
            buf = event.current_buffer
            buf.cancel_completion()
            buf.reset(append_to_history=False)

        return bindings

    # ------------------------------------------------------------------
    # Main loop
    # ------------------------------------------------------------------

    def run(self) -> None:
        """Start the interactive shell loop."""
        print(f"NOS — Network Operating System")
        print(f"Type 'configure' to enter configuration mode, '?' for help.\n")

        history = FileHistory(str(self._history_file))

        while True:
            completer = NOSCompleter(
                mode=self.mode,
                edit_path=self.conf_handler.edit_path,
                store=self.store,
            )
            key_bindings = self._build_key_bindings(completer)

            session: PromptSession = PromptSession(
                completer=completer,
                key_bindings=key_bindings,
                history=history,
                style=_STYLE,
                complete_while_typing=False,
                enable_history_search=True,
                mouse_support=False,
            )

            try:
                raw = session.prompt(self._build_prompt())
            except KeyboardInterrupt:
                # Ctrl+C at empty prompt → clear, continue
                continue
            except EOFError:
                # Ctrl+D → exit gracefully
                print("\nExiting NOS CLI.")
                break

            for line in raw.split("\n"):
                line = line.strip()
                if not line:
                    continue
                if self.mode == CLIMode.OPERATIONAL:
                    ok = self._run_operational(line)
                else:
                    ok = self._run_configure(line)
                if not ok:
                    break

    def _run_operational(self, line: str) -> bool:
        try:
            output = self.oper_handler.execute(line)
        except SystemExit:
            print("Exiting NOS CLI.")
            sys.exit(0)

        if output is None:
            # Switch to configure mode
            self.mode = CLIMode.CONFIGURE
            print("Entering configuration mode.\n")
            return True

        if output:
            print(output)
        return not (isinstance(output, str) and output.startswith("error:"))

    def _run_configure(self, line: str) -> bool:
        try:
            output = self.conf_handler.execute(line)
        except SystemExit:
            # exit / quit from configure mode → back to operational
            self.mode = CLIMode.OPERATIONAL
            self.conf_handler.edit_path = []
            print("\nExiting configuration mode.\n")
            return True
        except Exception as exc:
            print(f"error: {exc}")
            return False

        if output:
            print(output)

        # Confirm pending timer if user issued a plain 'commit'
        from nos.cli.parser import CommandParser, CommandType
        parsed = CommandParser().parse(line, CLIMode.CONFIGURE)
        if (
            parsed.command == CommandType.COMMIT
            and self.commit_engine.pending_confirmed
        ):
            self.commit_engine.confirm()
        return True


# ============================================================================
# Entry point
# ============================================================================

def main() -> None:
    """Entry point for the ``nos`` CLI command."""
    import logging
    logging.basicConfig(
        level=logging.WARNING,
        format="%(levelname)s %(name)s: %(message)s",
    )

    shell = NOSShell()
    try:
        shell.run()
    except KeyboardInterrupt:
        print("\nInterrupted.")
        sys.exit(0)


if __name__ == "__main__":
    main()
