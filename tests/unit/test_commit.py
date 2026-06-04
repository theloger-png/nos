"""Unit tests for nos.config.commit.CommitEngine."""
import copy
import json
import time
import threading
from pathlib import Path

import pytest

from nos.config.store import ConfigStore
from nos.config.commit import CommitEngine, CommitError, RollbackError
from nos.config.validator import ValidationResult


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture()
def tmp_store(tmp_path):
    (tmp_path / "config" / "rollback").mkdir(parents=True)
    running = tmp_path / "config" / "running.json"
    running.write_text('{"system": {"host_name": "r1"}}')
    store = ConfigStore(base_dir=tmp_path)
    return store


@pytest.fixture()
def engine(tmp_store):
    return CommitEngine(tmp_store)


# ---------------------------------------------------------------------------
# commit()
# ---------------------------------------------------------------------------

def test_commit_promotes_candidate_to_running(engine):
    engine.store.update_candidate(["system", "host_name"], "r2")
    engine.commit()
    assert engine.store.running["system"]["host_name"] == "r2"


def test_commit_persists_running_to_disk(engine):
    engine.store.update_candidate(["system", "host_name"], "r2")
    engine.commit()
    data = json.loads((engine.base_dir / "config" / "running.json").read_text())
    assert data["system"]["host_name"] == "r2"


def test_commit_creates_rollback_0(engine):
    engine.store.update_candidate(["system", "host_name"], "r2")
    engine.commit()
    path = engine._rollback_path(0)
    assert path.exists()
    data = json.loads(path.read_text())
    # rollback.0 holds the config BEFORE the commit
    assert data["system"]["host_name"] == "r1"


def test_commit_rotates_existing_rollbacks(engine):
    # seed rollback.0 and rollback.1
    engine._rollback_dir.mkdir(parents=True, exist_ok=True)
    engine._rollback_path(0).write_text('{"system": {"host_name": "r0"}}')
    engine._rollback_path(1).write_text('{"system": {"host_name": "r-1"}}')

    engine.store.update_candidate(["system", "host_name"], "r2")
    engine.commit()

    # old rollback.0 should shift to rollback.1
    data1 = json.loads(engine._rollback_path(1).read_text())
    assert data1["system"]["host_name"] == "r0"

    # old rollback.1 should shift to rollback.2
    data2 = json.loads(engine._rollback_path(2).read_text())
    assert data2["system"]["host_name"] == "r-1"


def test_commit_raises_on_invalid_candidate(engine):
    # mtu out of range — schema validation fails
    engine.store.update_candidate(["interfaces", "eth0", "mtu"], 99999)
    with pytest.raises(CommitError) as exc_info:
        engine.commit()
    assert exc_info.value.errors


def test_commit_does_not_rotate_rollbacks_on_validation_failure(engine):
    engine.store.update_candidate(["interfaces", "eth0", "mtu"], 99999)
    with pytest.raises(CommitError):
        engine.commit()
    # rollback.0 must NOT have been written
    assert not engine._rollback_path(0).exists()


def test_commit_keeps_candidate_unchanged_after_commit(engine):
    engine.store.update_candidate(["system", "host_name"], "r2")
    engine.commit()
    assert engine.store.candidate["system"]["host_name"] == "r2"


# ---------------------------------------------------------------------------
# rollback()
# ---------------------------------------------------------------------------

def test_rollback_loads_candidate_only(engine):
    engine._rollback_dir.mkdir(parents=True, exist_ok=True)
    engine._rollback_path(0).write_text('{"system": {"host_name": "old"}}')
    original_running = copy.deepcopy(engine.store.running)
    engine.rollback(0)
    assert engine.store.candidate["system"]["host_name"] == "old"
    assert engine.store.running == original_running


def test_rollback_restores_candidate(engine):
    engine._rollback_dir.mkdir(parents=True, exist_ok=True)
    engine._rollback_path(2).write_text('{"system": {"host_name": "two-back"}}')
    engine.rollback(2)
    assert engine.store.candidate["system"]["host_name"] == "two-back"


def test_rollback_does_not_modify_running(engine):
    engine._rollback_dir.mkdir(parents=True, exist_ok=True)
    engine._rollback_path(0).write_text('{"system": {"host_name": "old"}}')
    original_running = copy.deepcopy(engine.store.running)
    engine.rollback(0)
    data = json.loads((engine.base_dir / "config" / "running.json").read_text())
    assert data == original_running


def test_rollback_raises_if_checkpoint_missing(engine):
    with pytest.raises(RollbackError):
        engine.rollback(5)


def test_rollback_raises_for_out_of_range_index(engine):
    with pytest.raises(RollbackError):
        engine.rollback(50)
    with pytest.raises(RollbackError):
        engine.rollback(-1)


def test_rollback_then_commit_applies_checkpoint(engine):
    original = copy.deepcopy(engine.store.running)
    engine.store.update_candidate(["system", "host_name"], "r2")
    engine.commit()
    # running is now r2, candidate is r2
    # rollback loads the original checkpoint (r1) into candidate only
    engine.rollback(0)
    assert engine.store.running == {"system": {"host_name": "r2"}}
    assert engine.store.candidate == original
    # now commit applies the candidate
    engine.commit()
    assert engine.store.running == original


# ---------------------------------------------------------------------------
# commit_confirmed() + confirm()
# ---------------------------------------------------------------------------

def test_commit_confirmed_fires_commit(engine):
    engine.store.update_candidate(["system", "host_name"], "r2")
    engine.commit_confirmed(minutes=1)
    assert engine.store.running["system"]["host_name"] == "r2"
    engine.confirm()  # clean up timer


def test_commit_confirmed_sets_pending_flag(engine):
    engine.store.update_candidate(["system", "host_name"], "r2")
    engine.commit_confirmed(minutes=1)
    assert engine.pending_confirmed is True
    engine.confirm()


def test_confirm_cancels_timer(engine):
    engine.store.update_candidate(["system", "host_name"], "r2")
    engine.commit_confirmed(minutes=1)
    engine.confirm()
    assert engine.pending_confirmed is False


def test_confirm_is_idempotent(engine):
    engine.confirm()  # no timer active — should not raise
    assert engine.pending_confirmed is False


def test_commit_confirmed_auto_rollback_fires(engine, tmp_path):
    # Use a very short timeout to test the auto-rollback path
    engine._rollback_dir.mkdir(parents=True, exist_ok=True)
    engine.store.update_candidate(["system", "host_name"], "r2")
    engine.commit_confirmed(minutes=0)  # fires immediately (0 seconds)

    # Give the timer thread time to execute
    deadline = time.time() + 2.0
    while engine.store.running.get("system", {}).get("host_name") != "r1":
        if time.time() > deadline:
            break
        time.sleep(0.05)

    assert engine.store.running["system"]["host_name"] == "r1"


def test_commit_confirmed_no_rollback_after_confirm(engine):
    engine.store.update_candidate(["system", "host_name"], "r2")
    engine.commit_confirmed(minutes=1)
    engine.confirm()
    # host_name should remain r2 (no rollback happened)
    time.sleep(0.1)
    assert engine.store.running["system"]["host_name"] == "r2"


def test_second_commit_confirmed_cancels_first_timer(engine):
    engine.store.update_candidate(["system", "host_name"], "r2")
    engine.commit_confirmed(minutes=10)
    assert engine.pending_confirmed

    engine.store.update_candidate(["system", "host_name"], "r3")
    engine.commit_confirmed(minutes=10)  # should cancel previous timer
    assert engine.pending_confirmed
    engine.confirm()


# ---------------------------------------------------------------------------
# commit_check()
# ---------------------------------------------------------------------------

def test_commit_check_returns_valid_for_good_candidate(engine):
    result = engine.commit_check()
    assert result.is_valid


def test_commit_check_returns_invalid_for_bad_candidate(engine):
    engine.store.update_candidate(["interfaces", "eth0", "mtu"], 1)
    result = engine.commit_check()
    assert not result.is_valid


def test_commit_check_does_not_modify_running(engine):
    original_running = copy.deepcopy(engine.store.running)
    engine.store.update_candidate(["system", "host_name"], "r99")
    engine.commit_check()
    assert engine.store.running == original_running


def test_commit_check_phase2_hook_called(engine):
    called = []

    def _phase2(result):
        called.append(True)

    engine._phase2_check = _phase2
    engine.commit_check()
    assert called


def test_commit_check_phase2_not_called_when_phase1_fails(engine):
    called = []

    def _phase2(result):
        called.append(True)

    engine._phase2_check = _phase2
    engine.store.update_candidate(["interfaces", "eth0", "mtu"], 1)
    engine.commit_check()
    assert not called


# ---------------------------------------------------------------------------
# Rollback index boundary
# ---------------------------------------------------------------------------

def test_rollback_index_49_valid(engine):
    engine._rollback_dir.mkdir(parents=True, exist_ok=True)
    engine._rollback_path(49).write_text('{"system": {"host_name": "ancient"}}')
    original_running = copy.deepcopy(engine.store.running)
    engine.rollback(49)
    assert engine.store.candidate["system"]["host_name"] == "ancient"
    assert engine.store.running == original_running


def test_max_50_checkpoints_kept(engine):
    engine._rollback_dir.mkdir(parents=True, exist_ok=True)
    # Pre-populate all 50 slots
    for i in range(50):
        engine._rollback_path(i).write_text(f'{{"n": {i}}}')
    engine.store.update_candidate(["system", "host_name"], "new")
    engine.commit()
    # rollback.49 should now hold what was in rollback.48 before commit
    data = json.loads(engine._rollback_path(49).read_text())
    assert data["n"] == 48


# ---------------------------------------------------------------------------
# ConfigApplier integration
# ---------------------------------------------------------------------------

from unittest.mock import MagicMock, call


def _engine_with_applier(tmp_store):
    applier = MagicMock()
    engine = CommitEngine(tmp_store, applier=applier)
    return engine, applier


def test_applier_called_after_commit(tmp_store):
    engine, applier = _engine_with_applier(tmp_store)
    old_running = copy.deepcopy(engine.store.running)
    engine.store.update_candidate(["system", "host_name"], "r2")
    engine.commit()
    applier.apply.assert_called_once()
    old_arg, new_arg = applier.apply.call_args[0]
    assert old_arg == old_running
    assert new_arg["system"]["host_name"] == "r2"


def test_applier_receives_pre_commit_state_as_old(tmp_store):
    engine, applier = _engine_with_applier(tmp_store)
    engine.store.update_candidate(["system", "host_name"], "r2")
    engine.commit()
    old_arg, _ = applier.apply.call_args[0]
    # old_arg must reflect running BEFORE the commit (r1, not r2)
    assert old_arg["system"]["host_name"] == "r1"


def test_applier_not_called_when_commit_validation_fails(tmp_store):
    engine, applier = _engine_with_applier(tmp_store)
    engine.store.update_candidate(["interfaces", "eth0", "mtu"], 99999)
    with pytest.raises(CommitError):
        engine.commit()
    applier.apply.assert_not_called()


def test_applier_failure_does_not_raise_after_commit(tmp_store):
    engine, applier = _engine_with_applier(tmp_store)
    applier.apply.side_effect = Exception("driver exploded")
    engine.store.update_candidate(["system", "host_name"], "r2")
    engine.commit()  # must not raise
    assert engine.store.running["system"]["host_name"] == "r2"


def test_applier_not_called_after_rollback(tmp_store):
    engine, applier = _engine_with_applier(tmp_store)
    engine._rollback_dir.mkdir(parents=True, exist_ok=True)
    engine._rollback_path(0).write_text('{"system": {"host_name": "old"}}')
    engine.rollback(0)
    applier.apply.assert_not_called()


def test_applier_called_when_commit_after_rollback(tmp_store):
    engine, applier = _engine_with_applier(tmp_store)
    engine._rollback_dir.mkdir(parents=True, exist_ok=True)
    engine._rollback_path(0).write_text('{"system": {"host_name": "old"}}')
    old_running = copy.deepcopy(engine.store.running)
    engine.rollback(0)
    applier.apply.assert_not_called()
    # now commit applies the loaded candidate and calls applier
    engine.commit()
    applier.apply.assert_called_once()
    old_arg, new_arg = applier.apply.call_args[0]
    assert old_arg == old_running
    assert new_arg["system"]["host_name"] == "old"


def test_applier_not_called_when_rollback_checkpoint_missing(tmp_store):
    engine, applier = _engine_with_applier(tmp_store)
    with pytest.raises(RollbackError):
        engine.rollback(0)
    applier.apply.assert_not_called()


def test_no_applier_commit_works_as_before(tmp_store):
    engine = CommitEngine(tmp_store)  # no applier
    engine.store.update_candidate(["system", "host_name"], "r2")
    engine.commit()
    assert engine.store.running["system"]["host_name"] == "r2"


def test_commit_confirmed_applier_called(tmp_store):
    engine, applier = _engine_with_applier(tmp_store)
    engine.store.update_candidate(["system", "host_name"], "r2")
    engine.commit_confirmed(minutes=1)
    applier.apply.assert_called_once()
    engine.confirm()
