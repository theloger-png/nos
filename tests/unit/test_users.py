"""Unit tests for UserDriver, password hashing, show system login, and serializer."""
from __future__ import annotations

import json
import os
from unittest.mock import MagicMock, call, patch

import pytest

from nos.cli.commands.show.system import show_login
from nos.config.schema import LoginConfig, NOSConfig, SystemConfig, UserAuthentication, UserConfig, UserClassEnum
from nos.config.serializer import from_set_commands, to_set_commands
from nos.config.validator import ConfigValidator
from nos.drivers.kernel.users import UserDriver, _hash_password, _USERNAME_RE


# ---------------------------------------------------------------------------
# Password hashing
# ---------------------------------------------------------------------------

def test_hash_password_returns_sha512_crypt():
    hashed = _hash_password("mysecret")
    assert hashed.startswith("$6$"), f"Expected SHA-512 hash ($6$...), got: {hashed!r}"


def test_hash_password_different_salts():
    h1 = _hash_password("same")
    h2 = _hash_password("same")
    assert h1 != h2, "Two hashes of the same password must differ (random salt)"


def test_hash_password_never_stores_plaintext():
    plaintext = "supersecretpassword"
    hashed = _hash_password(plaintext)
    assert plaintext not in hashed


# ---------------------------------------------------------------------------
# UserDriver.apply() — subprocess calls
# ---------------------------------------------------------------------------

@patch("nos.drivers.kernel.users._user_exists", return_value=False)
@patch("nos.drivers.kernel.users.subprocess.run")
def test_apply_creates_new_user(mock_run, mock_exists, tmp_path):
    mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")
    driver = UserDriver()
    driver._MANAGED_USERS_FILE = str(tmp_path / "managed_users.json")

    with patch.object(driver, "_save_managed"), patch.object(driver, "_load_managed", return_value={}):
        driver._ensure_user("alice")

    useradd_calls = [c for c in mock_run.call_args_list
                     if c.args[0][:2] == ["sudo", "useradd"]]
    assert useradd_calls, "useradd should have been called"
    assert "alice" in useradd_calls[0].args[0]


@patch("nos.drivers.kernel.users._user_exists", return_value=True)
@patch("nos.drivers.kernel.users.subprocess.run")
def test_apply_skips_useradd_for_existing_user(mock_run, mock_exists, tmp_path):
    mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")
    driver = UserDriver()
    driver._ensure_user("alice")
    useradd_calls = [c for c in mock_run.call_args_list
                     if any("useradd" in str(a) for a in c.args[0])]
    assert not useradd_calls, "useradd must not be called for existing user"


@patch("nos.drivers.kernel.users.subprocess.run")
def test_apply_sets_password_via_chpasswd(mock_run, tmp_path):
    mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")
    driver = UserDriver()
    driver._set_password("bob", "$6$fakehash$abc")
    chpasswd_calls = [c for c in mock_run.call_args_list
                      if "chpasswd" in c.args[0]]
    assert chpasswd_calls, "chpasswd should have been called"
    assert chpasswd_calls[0].kwargs.get("input", "") == "bob:$6$fakehash$abc"


@patch("nos.drivers.kernel.users._run")
def test_apply_super_user_added_to_sudo_group(mock_run):
    mock_run.return_value = True
    UserDriver()._set_groups("carol", "super-user")
    mock_run.assert_called_once()
    groups_arg = mock_run.call_args.args[0]
    assert "sudo" in groups_arg[groups_arg.index("-aG") + 1]


@patch("nos.drivers.kernel.users._run")
def test_apply_operator_not_added_to_sudo_group(mock_run):
    mock_run.return_value = True
    UserDriver()._set_groups("dave", "operator")
    groups_arg = mock_run.call_args.args[0]
    groups_str = groups_arg[groups_arg.index("-aG") + 1]
    assert "sudo" not in groups_str


@patch("nos.drivers.kernel.users._run")
def test_apply_removes_managed_user(mock_run):
    mock_run.return_value = True
    UserDriver()._remove_user("olduser")
    mock_run.assert_called_once()
    assert "userdel" in mock_run.call_args.args[0]
    assert "olduser" in mock_run.call_args.args[0]


# ---------------------------------------------------------------------------
# managed_users.json tracking
# ---------------------------------------------------------------------------

def test_load_managed_returns_empty_when_file_missing(tmp_path):
    driver = UserDriver()
    with patch("nos.drivers.kernel.users._MANAGED_USERS_FILE",
               str(tmp_path / "nonexistent.json")):
        result = driver._load_managed()
    assert result == {}


def test_save_and_load_managed(tmp_path):
    path = str(tmp_path / "managed_users.json")
    driver = UserDriver()
    with patch("nos.drivers.kernel.users._MANAGED_USERS_FILE", path):
        driver._save_managed({"alice": True, "bob": True})
        loaded = driver._load_managed()
    assert loaded == {"alice": True, "bob": True}


@patch("nos.drivers.kernel.users._user_exists", return_value=False)
@patch("nos.drivers.kernel.users.subprocess.run")
@patch("nos.drivers.kernel.users._run")
def test_apply_full_flow_tracks_managed_users(mock_run, mock_sub_run, mock_exists, tmp_path):
    managed_path = str(tmp_path / "managed_users.json")
    mock_sub_run.return_value = MagicMock(returncode=0, stdout="", stderr="")
    mock_run.return_value = True

    login_config = {
        "user": {
            "alice": {"user_class": "super-user", "authentication": {"password": "$6$abc"}}
        }
    }

    driver = UserDriver()
    with patch("nos.drivers.kernel.users._MANAGED_USERS_FILE", managed_path):
        driver.apply(login_config)
        with open(managed_path) as fh:
            managed = json.load(fh)
    assert "alice" in managed


@patch("nos.drivers.kernel.users._user_exists", return_value=False)
@patch("nos.drivers.kernel.users.subprocess.run")
@patch("nos.drivers.kernel.users._run")
def test_apply_removes_user_not_in_config(mock_run, mock_sub_run, mock_exists, tmp_path):
    """User present in managed file but absent from config must be deleted."""
    managed_path = str(tmp_path / "managed_users.json")
    with open(managed_path, "w") as fh:
        json.dump({"olduser": True}, fh)

    mock_sub_run.return_value = MagicMock(returncode=0, stdout="", stderr="")
    mock_run.return_value = True

    driver = UserDriver()
    with patch("nos.drivers.kernel.users._MANAGED_USERS_FILE", managed_path):
        with patch.object(driver, "_is_logged_in", return_value=False):
            driver.apply({"user": {}})
        with open(managed_path) as fh:
            managed = json.load(fh)

    userdel_calls = [c for c in mock_run.call_args_list
                     if "userdel" in c.args[0]]
    assert userdel_calls, "userdel must be called for removed managed user"
    assert "olduser" not in managed


@patch("nos.drivers.kernel.users._user_exists", return_value=False)
@patch("nos.drivers.kernel.users.subprocess.run")
@patch("nos.drivers.kernel.users._run")
def test_apply_skips_removal_for_logged_in_user(mock_run, mock_sub_run, mock_exists, tmp_path):
    managed_path = str(tmp_path / "managed_users.json")
    with open(managed_path, "w") as fh:
        json.dump({"activesession": True}, fh)

    mock_sub_run.return_value = MagicMock(returncode=0, stdout="", stderr="")
    mock_run.return_value = True

    driver = UserDriver()
    with patch("nos.drivers.kernel.users._MANAGED_USERS_FILE", managed_path):
        with patch.object(driver, "_is_logged_in", return_value=True):
            driver.apply({"user": {}})

    userdel_calls = [c for c in mock_run.call_args_list
                     if "userdel" in c.args[0]]
    assert not userdel_calls, "userdel must not be called while user is logged in"


# ---------------------------------------------------------------------------
# show system login
# ---------------------------------------------------------------------------

def test_show_login_empty():
    output = show_login({})
    assert "No login users configured" in output


def test_show_login_single_user():
    login = {
        "user": {
            "admin": {"user_class": "super-user", "authentication": {}}
        }
    }
    output = show_login(login)
    assert "admin" in output
    assert "super-user" in output
    assert "active" in output


def test_show_login_multiple_users_sorted():
    login = {
        "user": {
            "zoe": {"user_class": "operator", "authentication": {}},
            "alice": {"user_class": "read-only", "authentication": {}},
        }
    }
    output = show_login(login)
    lines = output.splitlines()
    user_lines = [l for l in lines if "alice" in l or "zoe" in l]
    assert user_lines[0].startswith("alice"), "Users must be sorted alphabetically"


def test_show_login_user_class_hyphenated():
    login = {
        "user": {
            "op1": {"user_class": "read_only", "authentication": {}}
        }
    }
    output = show_login(login)
    assert "read-only" in output


# ---------------------------------------------------------------------------
# Validator — username and user_class checks
# ---------------------------------------------------------------------------

def test_validator_rejects_invalid_username():
    config = {
        "system": {
            "login": {
                "user": {
                    "bad user!": {"user_class": "operator"}
                }
            }
        }
    }
    result = ConfigValidator().validate(config)
    assert not result.is_valid
    assert any("Invalid username" in str(e) for e in result.errors)


def test_validator_rejects_missing_user_class():
    config = {
        "system": {
            "login": {
                "user": {
                    "validname": {}
                }
            }
        }
    }
    result = ConfigValidator().validate(config)
    assert not result.is_valid
    assert any("user_class is required" in str(e) for e in result.errors)


def test_validator_accepts_valid_user():
    config = {
        "system": {
            "login": {
                "user": {
                    "admin": {"user_class": "super-user"}
                }
            }
        }
    }
    result = ConfigValidator().validate(config)
    assert result.is_valid


# ---------------------------------------------------------------------------
# Serializer round-trip
# ---------------------------------------------------------------------------

def test_serializer_roundtrip_authentication_password():
    config = {
        "system": {
            "login": {
                "user": {
                    "admin": {
                        "user_class": "super-user",
                        "authentication": {
                            "password": "$6$abc$xyz"
                        }
                    }
                }
            }
        }
    }
    cmds = to_set_commands(config)
    # password line must be present
    pw_cmds = [c for c in cmds if "authentication" in c and "password" in c]
    assert pw_cmds, f"Expected authentication password command, got: {cmds}"
    assert '"$6$abc$xyz"' in pw_cmds[0]

    # round-trip
    rebuilt = from_set_commands(cmds)
    pw = rebuilt["system"]["login"]["user"]["admin"]["authentication"]["password"]
    assert pw == "$6$abc$xyz"


def test_serializer_emits_correct_set_command_path():
    config = {
        "system": {
            "login": {
                "user": {
                    "admin": {"user_class": "super-user"}
                }
            }
        }
    }
    cmds = to_set_commands(config)
    class_cmd = [c for c in cmds if "user-class" in c or "user_class" in c]
    assert class_cmd, f"Expected user class command, got: {cmds}"
    assert "super-user" in class_cmd[0]
