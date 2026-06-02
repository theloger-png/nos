"""Unit tests for multi-line paste support in NOSShell."""
from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch, call

import pytest

from nos.cli.shell import NOSShell
from nos.cli.parser import CLIMode
from nos.config.store import ConfigStore


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_shell(tmp_path: Path) -> NOSShell:
    store = ConfigStore(base_dir=tmp_path)
    with (
        patch("nos.cli.shell.PFEManager"),
        patch("nos.cli.shell.KernelDriver"),
        patch("nos.cli.shell.FRRClient"),
        patch("nos.cli.shell.ConfigApplier"),
        patch("nos.cli.shell.CommitEngine"),
    ):
        shell = NOSShell(store=store, username="test", hostname="nos01",
                         history_file=tmp_path / ".nos_history")
    return shell


# ---------------------------------------------------------------------------
# _run_operational return values
# ---------------------------------------------------------------------------

class TestRunOperational:
    def test_returns_true_on_success(self, tmp_path):
        shell = _make_shell(tmp_path)
        shell.oper_handler = MagicMock()
        shell.oper_handler.execute.return_value = "some output"
        assert shell._run_operational("show route") is True

    def test_returns_false_on_error_output(self, tmp_path):
        shell = _make_shell(tmp_path)
        shell.oper_handler = MagicMock()
        shell.oper_handler.execute.return_value = "error: unknown command"
        assert shell._run_operational("bad command") is False

    def test_returns_true_on_mode_switch(self, tmp_path):
        shell = _make_shell(tmp_path)
        shell.oper_handler = MagicMock()
        shell.oper_handler.execute.return_value = None
        result = shell._run_operational("configure")
        assert result is True
        assert shell.mode == CLIMode.CONFIGURE

    def test_returns_true_on_empty_output(self, tmp_path):
        shell = _make_shell(tmp_path)
        shell.oper_handler = MagicMock()
        shell.oper_handler.execute.return_value = ""
        assert shell._run_operational("show route") is True


# ---------------------------------------------------------------------------
# _run_configure return values
# ---------------------------------------------------------------------------

class TestRunConfigure:
    def test_returns_true_on_success(self, tmp_path):
        shell = _make_shell(tmp_path)
        shell.conf_handler = MagicMock()
        shell.conf_handler.execute.return_value = ""
        shell.commit_engine = MagicMock()
        shell.commit_engine.pending_confirmed = False
        shell.mode = CLIMode.CONFIGURE
        assert shell._run_configure("set system host-name r1") is True

    def test_returns_false_on_exception(self, tmp_path):
        shell = _make_shell(tmp_path)
        shell.conf_handler = MagicMock()
        shell.conf_handler.execute.side_effect = ValueError("bad value")
        shell.mode = CLIMode.CONFIGURE
        assert shell._run_configure("set bad stuff") is False

    def test_returns_true_on_exit(self, tmp_path):
        shell = _make_shell(tmp_path)
        shell.conf_handler = MagicMock()
        shell.conf_handler.execute.side_effect = SystemExit(0)
        shell.mode = CLIMode.CONFIGURE
        result = shell._run_configure("exit")
        assert result is True
        assert shell.mode == CLIMode.OPERATIONAL


# ---------------------------------------------------------------------------
# Multi-line paste via run() loop
# ---------------------------------------------------------------------------

class TestMultiLinePaste:
    def _run_with_input(self, shell: NOSShell, inputs: list[str]) -> None:
        """Drive shell.run() with a sequence of prompt() return values."""
        side_effects = inputs + [EOFError()]
        with patch("nos.cli.shell.PromptSession") as MockSession:
            session_inst = MagicMock()
            MockSession.return_value = session_inst
            session_inst.prompt.side_effect = side_effects
            shell.run()

    def test_single_line_unchanged(self, tmp_path, capsys):
        shell = _make_shell(tmp_path)
        shell.oper_handler = MagicMock()
        shell.oper_handler.execute.return_value = "route output"
        self._run_with_input(shell, ["show route"])
        out = capsys.readouterr().out
        assert "route output" in out

    def test_multiline_paste_runs_all_lines(self, tmp_path, capsys):
        shell = _make_shell(tmp_path)
        shell.oper_handler = MagicMock()
        shell.oper_handler.execute.side_effect = ["out1", "out2", "out3"]
        self._run_with_input(shell, ["cmd1\ncmd2\ncmd3"])
        assert shell.oper_handler.execute.call_count == 3
        shell.oper_handler.execute.assert_any_call("cmd1")
        shell.oper_handler.execute.assert_any_call("cmd2")
        shell.oper_handler.execute.assert_any_call("cmd3")

    def test_multiline_stops_on_error(self, tmp_path):
        shell = _make_shell(tmp_path)
        shell.oper_handler = MagicMock()
        shell.oper_handler.execute.side_effect = [
            "error: bad command",  # cmd1 fails
            "ok",                  # cmd2 should not run
        ]
        self._run_with_input(shell, ["cmd1\ncmd2"])
        assert shell.oper_handler.execute.call_count == 1

    def test_multiline_skips_empty_lines(self, tmp_path):
        shell = _make_shell(tmp_path)
        shell.oper_handler = MagicMock()
        shell.oper_handler.execute.return_value = "ok"
        self._run_with_input(shell, ["cmd1\n\n   \ncmd2"])
        assert shell.oper_handler.execute.call_count == 2
        shell.oper_handler.execute.assert_any_call("cmd1")
        shell.oper_handler.execute.assert_any_call("cmd2")

    def test_multiline_configure_mode(self, tmp_path):
        shell = _make_shell(tmp_path)
        shell.mode = CLIMode.CONFIGURE
        shell.conf_handler = MagicMock()
        shell.conf_handler.execute.return_value = ""
        shell.commit_engine = MagicMock()
        shell.commit_engine.pending_confirmed = False
        self._run_with_input(shell, [
            "set system host-name r1\nset interfaces eth0 description uplink"
        ])
        assert shell.conf_handler.execute.call_count == 2

    def test_multiline_configure_stops_on_error(self, tmp_path):
        shell = _make_shell(tmp_path)
        shell.mode = CLIMode.CONFIGURE
        shell.conf_handler = MagicMock()
        shell.conf_handler.execute.side_effect = [
            ValueError("invalid"),  # first line raises
            "",                     # second should not run
        ]
        self._run_with_input(shell, ["bad set\nset system host-name r1"])
        assert shell.conf_handler.execute.call_count == 1

    def test_mode_switch_within_multiline(self, tmp_path):
        """configure\\nset ... should switch to configure mode then run the set."""
        shell = _make_shell(tmp_path)
        assert shell.mode == CLIMode.OPERATIONAL
        shell.oper_handler = MagicMock()
        shell.oper_handler.execute.return_value = None  # "configure" → mode switch
        shell.conf_handler = MagicMock()
        shell.conf_handler.execute.return_value = ""
        shell.commit_engine = MagicMock()
        shell.commit_engine.pending_confirmed = False
        self._run_with_input(shell, ["configure\nset system host-name r1"])
        assert shell.oper_handler.execute.call_count == 1
        assert shell.conf_handler.execute.call_count == 1
        shell.conf_handler.execute.assert_called_with("set system host-name r1")
