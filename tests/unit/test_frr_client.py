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
    conf_path = tmp_path / "frr.conf"
    monkeypatch.setattr("nos.drivers.frr.client._FRR_CONF", conf_path)
    # frr-reload.py doesn't exist → fallback branch
    monkeypatch.setattr(
        "nos.drivers.frr.client._FRR_RELOAD",
        str(tmp_path / "nonexistent-reload.py"),
    )

    def mock_run(cmd, *args, **kwargs):
        # When sudo tee is called, actually write the file
        if cmd[0:2] == ["sudo", "tee"]:
            conf_path.parent.mkdir(parents=True, exist_ok=True)
            conf_path.write_bytes(kwargs.get("input", b""))
        return _ok()

    run_fn = MagicMock(side_effect=mock_run)
    client = FRRClient(run_fn=run_fn)
    client.write_frr_conf("frr version 8.0\nhostname nos01\n!")

    assert conf_path.exists()
    assert "nos01" in conf_path.read_text()


def test_write_frr_conf_uses_frr_reload_when_available(tmp_path, monkeypatch):
    conf_path = tmp_path / "frr.conf"
    monkeypatch.setattr("nos.drivers.frr.client._FRR_CONF", conf_path)

    reload_script = tmp_path / "frr-reload.py"
    reload_script.write_text("# stub")
    monkeypatch.setattr("nos.drivers.frr.client._FRR_RELOAD", str(reload_script))

    run_fn = MagicMock(return_value=_ok())
    client = FRRClient(run_fn=run_fn)
    client.write_frr_conf("!")

    # Second call should be frr-reload.py
    assert run_fn.call_count == 2
    reload_call = run_fn.call_args_list[1]
    assert "frr-reload.py" in " ".join(reload_call[0][0])


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

    # Second call should be vtysh -f
    assert run_fn.call_count == 2
    vtysh_call = run_fn.call_args_list[1]
    assert "-f" in vtysh_call[0][0]


def test_write_frr_conf_raises_on_reload_failure(tmp_path, monkeypatch):
    conf_path = tmp_path / "frr.conf"
    monkeypatch.setattr("nos.drivers.frr.client._FRR_CONF", conf_path)
    monkeypatch.setattr(
        "nos.drivers.frr.client._FRR_RELOAD",
        str(tmp_path / "absent.py"),
    )

    # First call (sudo tee) succeeds, second call (vtysh -f) fails
    it = iter([_ok(), _fail(rc=1, stderr="vtysh error")])
    run_fn = MagicMock(side_effect=lambda *a, **kw: next(it))
    client = FRRClient(run_fn=run_fn)
    with pytest.raises(FRRClientError):
        client.write_frr_conf("!")


# ---------------------------------------------------------------------------
# sync_daemons
# ---------------------------------------------------------------------------

_DAEMONS_CONTENT = """\
bgpd=no
ospfd=no
ospf6d=no
isisd=no
ripd=no
vtysh_enable=yes
zebra_options="  -A 127.0.0.1"
"""


def _daemons_file(tmp_path, content=_DAEMONS_CONTENT):
    f = tmp_path / "daemons"
    f.write_text(content)
    return f


class TestSyncDaemons:
    def _client_and_run(self, side_effects=None):
        """Return (client, run_fn).  side_effects is a list of return values."""
        if side_effects is None:
            side_effects = [_ok(), _ok()]
        it = iter(side_effects)
        run_fn = MagicMock(side_effect=lambda *a, **kw: next(it))
        return FRRClient(run_fn=run_fn), run_fn

    # -- change detection ----------------------------------------------------

    def test_enables_bgpd_when_bgp_active(self, tmp_path, monkeypatch):
        daemons = _daemons_file(tmp_path)
        monkeypatch.setattr("nos.drivers.frr.client._FRR_DAEMONS", daemons)
        client, run_fn = self._client_and_run()

        client.sync_daemons({"bgp"})

        tee_call = run_fn.call_args_list[0]
        assert tee_call[0][0] == ["sudo", "tee", str(daemons)]
        assert "bgpd=yes" in tee_call[1]["input"]

    def test_enables_isisd_when_isis_active(self, tmp_path, monkeypatch):
        daemons = _daemons_file(tmp_path)
        monkeypatch.setattr("nos.drivers.frr.client._FRR_DAEMONS", daemons)
        client, run_fn = self._client_and_run()

        client.sync_daemons({"isis"})

        tee_input = run_fn.call_args_list[0][1]["input"]
        assert "isisd=yes" in tee_input

    def test_enables_ospfd_when_ospf_active(self, tmp_path, monkeypatch):
        daemons = _daemons_file(tmp_path)
        monkeypatch.setattr("nos.drivers.frr.client._FRR_DAEMONS", daemons)
        client, run_fn = self._client_and_run()

        client.sync_daemons({"ospf"})

        tee_input = run_fn.call_args_list[0][1]["input"]
        assert "ospfd=yes" in tee_input

    def test_enables_multiple_daemons_simultaneously(self, tmp_path, monkeypatch):
        daemons = _daemons_file(tmp_path)
        monkeypatch.setattr("nos.drivers.frr.client._FRR_DAEMONS", daemons)
        client, run_fn = self._client_and_run()

        client.sync_daemons({"bgp", "isis"})

        tee_input = run_fn.call_args_list[0][1]["input"]
        assert "bgpd=yes" in tee_input
        assert "isisd=yes" in tee_input

    def test_disables_bgpd_when_removed(self, tmp_path, monkeypatch):
        daemons = _daemons_file(tmp_path, "bgpd=yes\nisisd=no\nospfd=no\n")
        monkeypatch.setattr("nos.drivers.frr.client._FRR_DAEMONS", daemons)
        client, run_fn = self._client_and_run()

        client.sync_daemons(set())

        tee_input = run_fn.call_args_list[0][1]["input"]
        assert "bgpd=no" in tee_input

    def test_no_subprocess_call_when_already_correct(self, tmp_path, monkeypatch):
        daemons = _daemons_file(tmp_path, "bgpd=yes\nisisd=no\nospfd=no\n")
        monkeypatch.setattr("nos.drivers.frr.client._FRR_DAEMONS", daemons)
        client, run_fn = self._client_and_run()

        client.sync_daemons({"bgp"})

        run_fn.assert_not_called()

    def test_no_subprocess_call_when_all_disabled_and_empty_set(self, tmp_path, monkeypatch):
        daemons = _daemons_file(tmp_path)  # all =no already
        monkeypatch.setattr("nos.drivers.frr.client._FRR_DAEMONS", daemons)
        client, run_fn = self._client_and_run()

        client.sync_daemons(set())

        run_fn.assert_not_called()

    # -- restart behaviour ---------------------------------------------------

    def test_restarts_frr_after_change(self, tmp_path, monkeypatch):
        daemons = _daemons_file(tmp_path)
        monkeypatch.setattr("nos.drivers.frr.client._FRR_DAEMONS", daemons)
        client, run_fn = self._client_and_run()

        client.sync_daemons({"bgp"})

        assert run_fn.call_count == 2
        restart_call = run_fn.call_args_list[1]
        assert restart_call[0][0] == ["sudo", "systemctl", "restart", "frr"]

    def test_no_restart_when_no_change(self, tmp_path, monkeypatch):
        daemons = _daemons_file(tmp_path, "bgpd=yes\nisisd=no\nospfd=no\n")
        monkeypatch.setattr("nos.drivers.frr.client._FRR_DAEMONS", daemons)
        client, run_fn = self._client_and_run()

        client.sync_daemons({"bgp"})

        run_fn.assert_not_called()

    # -- error handling ------------------------------------------------------

    def test_graceful_on_daemons_file_missing(self, tmp_path, monkeypatch):
        monkeypatch.setattr(
            "nos.drivers.frr.client._FRR_DAEMONS", tmp_path / "nonexistent"
        )
        client, run_fn = self._client_and_run()

        client.sync_daemons({"bgp"})  # must not raise

        run_fn.assert_not_called()

    def test_graceful_on_write_failure(self, tmp_path, monkeypatch):
        daemons = _daemons_file(tmp_path)
        monkeypatch.setattr("nos.drivers.frr.client._FRR_DAEMONS", daemons)
        client, run_fn = self._client_and_run(
            side_effects=[_fail(rc=1, stderr="sudo: permission denied"), _ok()]
        )

        client.sync_daemons({"bgp"})  # must not raise

        # tee was attempted, restart must NOT have been called
        assert run_fn.call_count == 1
        assert "tee" in run_fn.call_args_list[0][0][0]

    def test_raises_when_systemctl_restart_fails(self, tmp_path, monkeypatch):
        daemons = _daemons_file(tmp_path)
        monkeypatch.setattr("nos.drivers.frr.client._FRR_DAEMONS", daemons)
        client, run_fn = self._client_and_run(
            side_effects=[_ok(), _fail(rc=1, stderr="Failed to restart frr.service")]
        )

        with pytest.raises(FRRClientError, match="systemctl restart frr failed"):
            client.sync_daemons({"bgp"})

    # -- output integrity ---------------------------------------------------

    def test_non_daemon_lines_preserved(self, tmp_path, monkeypatch):
        content = "bgpd=no\nospfd=no\nisisd=no\nvtysh_enable=yes\nzebra_options=\"-A 127.0.0.1\"\n"
        daemons = _daemons_file(tmp_path, content)
        monkeypatch.setattr("nos.drivers.frr.client._FRR_DAEMONS", daemons)
        client, run_fn = self._client_and_run()

        client.sync_daemons({"bgp"})

        tee_input = run_fn.call_args_list[0][1]["input"]
        assert "vtysh_enable=yes" in tee_input
        assert 'zebra_options="-A 127.0.0.1"' in tee_input

    def test_trailing_newline_preserved(self, tmp_path, monkeypatch):
        content = "bgpd=no\nisisd=no\nospfd=no\n"
        daemons = _daemons_file(tmp_path, content)
        monkeypatch.setattr("nos.drivers.frr.client._FRR_DAEMONS", daemons)
        client, run_fn = self._client_and_run()

        client.sync_daemons({"bgp"})

        tee_input = run_fn.call_args_list[0][1]["input"]
        assert tee_input.endswith("\n")
