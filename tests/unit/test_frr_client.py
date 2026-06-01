"""Unit tests for nos.drivers.frr.client.FRRClient."""
import subprocess
from pathlib import Path
from unittest.mock import MagicMock, call, patch

import pytest

from nos.drivers.frr.client import FRRClient, FRRClientError, _FRR_CONF, _VTYSH_BIN


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _ok(stdout="", stderr=""):
    r = MagicMock()
    r.returncode = 0
    r.stdout = stdout
    r.stderr = stderr
    return r


def _fail(rc=1, stderr="error message"):
    r = MagicMock()
    r.returncode = rc
    r.stdout = ""
    r.stderr = stderr
    return r


# ---------------------------------------------------------------------------
# send_config
# ---------------------------------------------------------------------------

def test_send_config_issues_vtysh_commands():
    run_fn = MagicMock(return_value=_ok())
    client = FRRClient(run_fn=run_fn)

    client.send_config(["router isis default", "net 49.0001.0000.0101.0101.00"])

    args = run_fn.call_args[0][0]
    assert args[0] == _VTYSH_BIN
    assert "configure terminal" in args
    assert "router isis default" in args
    assert "net 49.0001.0000.0101.0101.00" in args
    assert "end" in args


def test_send_config_returns_stdout():
    run_fn = MagicMock(return_value=_ok(stdout="ok\n"))
    client = FRRClient(run_fn=run_fn)
    result = client.send_config(["no router bgp"])
    assert result == "ok\n"


def test_send_config_raises_on_nonzero():
    run_fn = MagicMock(return_value=_fail(rc=1, stderr="% Unknown command"))
    client = FRRClient(run_fn=run_fn)
    with pytest.raises(FRRClientError, match="Unknown command"):
        client.send_config(["no router bogus 99999"])


def test_send_config_empty_list():
    run_fn = MagicMock(return_value=_ok())
    client = FRRClient(run_fn=run_fn)
    client.send_config([])
    run_fn.assert_called_once()


# ---------------------------------------------------------------------------
# show
# ---------------------------------------------------------------------------

def test_show_returns_output():
    run_fn = MagicMock(return_value=_ok(stdout="BGP summary...\n"))
    client = FRRClient(run_fn=run_fn)
    out = client.show("show bgp summary")
    assert "BGP summary" in out


def test_show_calls_vtysh_correctly():
    run_fn = MagicMock(return_value=_ok())
    client = FRRClient(run_fn=run_fn)
    client.show("show isis adjacency")
    run_fn.assert_called_once_with(
        [_VTYSH_BIN, "-c", "show isis adjacency"],
        capture_output=True,
        text=True,
    )


def test_show_raises_on_nonzero():
    run_fn = MagicMock(return_value=_fail(rc=1, stderr="% Command incomplete"))
    client = FRRClient(run_fn=run_fn)
    with pytest.raises(FRRClientError, match="Command incomplete"):
        client.show("show bogus")


# ---------------------------------------------------------------------------
# write_frr_conf (with reload)
# ---------------------------------------------------------------------------

def test_write_frr_conf_writes_file(tmp_path, monkeypatch):
    monkeypatch.setattr("nos.drivers.frr.client._FRR_CONF", tmp_path / "frr.conf")
    # frr-reload.py doesn't exist → fallback branch
    monkeypatch.setattr(
        "nos.drivers.frr.client._FRR_RELOAD",
        str(tmp_path / "nonexistent-reload.py"),
    )

    run_fn = MagicMock(return_value=_ok())
    client = FRRClient(run_fn=run_fn)
    client.write_frr_conf("frr version 8.0\nhostname nos01\n!")

    conf = tmp_path / "frr.conf"
    assert conf.exists()
    assert "nos01" in conf.read_text()


def test_write_frr_conf_uses_frr_reload_when_available(tmp_path, monkeypatch):
    conf_path = tmp_path / "frr.conf"
    monkeypatch.setattr("nos.drivers.frr.client._FRR_CONF", conf_path)

    reload_script = tmp_path / "frr-reload.py"
    reload_script.write_text("# stub")
    monkeypatch.setattr("nos.drivers.frr.client._FRR_RELOAD", str(reload_script))

    run_fn = MagicMock(return_value=_ok())
    client = FRRClient(run_fn=run_fn)
    client.write_frr_conf("!")

    cmd_used = run_fn.call_args[0][0]
    assert "frr-reload.py" in " ".join(cmd_used)


def test_write_frr_conf_fallback_vtysh_f(tmp_path, monkeypatch):
    conf_path = tmp_path / "frr.conf"
    monkeypatch.setattr("nos.drivers.frr.client._FRR_CONF", conf_path)
    monkeypatch.setattr(
        "nos.drivers.frr.client._FRR_RELOAD",
        str(tmp_path / "absent.py"),
    )

    run_fn = MagicMock(return_value=_ok())
    client = FRRClient(run_fn=run_fn)
    client.write_frr_conf("!")

    cmd_used = run_fn.call_args[0][0]
    assert "-f" in cmd_used


def test_write_frr_conf_raises_on_reload_failure(tmp_path, monkeypatch):
    conf_path = tmp_path / "frr.conf"
    monkeypatch.setattr("nos.drivers.frr.client._FRR_CONF", conf_path)
    monkeypatch.setattr(
        "nos.drivers.frr.client._FRR_RELOAD",
        str(tmp_path / "absent.py"),
    )

    run_fn = MagicMock(return_value=_fail(rc=1, stderr="vtysh error"))
    client = FRRClient(run_fn=run_fn)
    with pytest.raises(FRRClientError):
        client.write_frr_conf("!")
