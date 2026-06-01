from __future__ import annotations

import copy
import json
import logging
import shutil
import threading
from pathlib import Path
from typing import Optional

from nos.config.store import ConfigStore
from nos.config.validator import ConfigValidator, ValidationResult

logger = logging.getLogger(__name__)

_MAX_ROLLBACKS = 50


class CommitError(Exception):
    def __init__(self, errors: list) -> None:
        self.errors = errors
        super().__init__(f"Commit validation failed: {errors}")


class RollbackError(Exception):
    pass


class CommitEngine:
    """Manages commit, rollback, and commit-confirmed workflows.

    Wraps a ConfigStore and adds:
    - Rollback checkpoint rotation (rollback.0 – rollback.49)
    - commit_confirmed() with background auto-rollback timer
    - commit_check() Phase-2 dry-run stub
    """

    def __init__(
        self,
        store: ConfigStore,
        base_dir: Optional[Path] = None,
        validator: Optional[ConfigValidator] = None,
    ) -> None:
        self.store = store
        self.base_dir: Path = Path(base_dir) if base_dir is not None else store.base_dir
        self._rollback_dir = self.base_dir / "config" / "rollback"
        self._validator = validator or ConfigValidator()
        self._confirmed_timer: Optional[threading.Timer] = None
        self._timer_lock = threading.Lock()

    # ------------------------------------------------------------------
    # Paths
    # ------------------------------------------------------------------

    def _rollback_path(self, n: int) -> Path:
        return self._rollback_dir / f"rollback.{n}.json"

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _rotate_rollbacks(self) -> None:
        """Shift rollback.0–48 → rollback.1–49, then copy running → rollback.0."""
        self._rollback_dir.mkdir(parents=True, exist_ok=True)
        for n in range(_MAX_ROLLBACKS - 2, -1, -1):
            src = self._rollback_path(n)
            dst = self._rollback_path(n + 1)
            if src.exists():
                shutil.copy2(src, dst)
        running_path = self.store._running_path
        if running_path.exists():
            shutil.copy2(running_path, self._rollback_path(0))
        else:
            # Write empty config as checkpoint so rollback.0 always exists post-commit
            with open(self._rollback_path(0), "w") as fh:
                json.dump(self.store.running, fh, indent=2)

    def _do_commit(self) -> None:
        """Internal: rotate rollbacks and promote candidate → running."""
        self._rotate_rollbacks()
        self.store.commit()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def commit(self) -> None:
        """Validate candidate and promote it to running with rollback rotation.

        Raises CommitError if Phase-1 validation fails.
        """
        result = self._validator.validate(self.store.candidate)
        if not result.is_valid:
            raise CommitError(result.errors)
        self._do_commit()
        logger.info("Commit successful")

    def rollback(self, n: int) -> None:
        """Revert running and candidate to checkpoint rollback.N.

        Raises RollbackError if the checkpoint does not exist.
        """
        if not (0 <= n < _MAX_ROLLBACKS):
            raise RollbackError(f"Rollback index must be 0–{_MAX_ROLLBACKS - 1}, got {n}")
        path = self._rollback_path(n)
        if not path.exists():
            raise RollbackError(f"Checkpoint rollback.{n} does not exist")
        with open(path) as fh:
            config = json.load(fh)
        self.store.running = copy.deepcopy(config)
        self.store.candidate = copy.deepcopy(config)
        self.store.save_running()
        self.store.save_candidate()
        logger.info("Rolled back to checkpoint %d", n)

    def commit_confirmed(self, minutes: int) -> None:
        """Commit and schedule an automatic rollback after *minutes* minutes.

        Call confirm() before the timer fires to cancel the auto-rollback.
        A second call to commit_confirmed() cancels any pending timer first.
        """
        with self._timer_lock:
            if self._confirmed_timer is not None:
                self._confirmed_timer.cancel()
                self._confirmed_timer = None

        self.commit()

        timer = threading.Timer(minutes * 60, self._auto_rollback)
        timer.daemon = True
        with self._timer_lock:
            self._confirmed_timer = timer
        timer.start()
        logger.info("Commit confirmed — will auto-rollback in %d minute(s)", minutes)

    def confirm(self) -> None:
        """Cancel a pending commit-confirmed auto-rollback timer."""
        with self._timer_lock:
            if self._confirmed_timer is not None:
                self._confirmed_timer.cancel()
                self._confirmed_timer = None
                logger.info("Commit confirmed — auto-rollback cancelled")

    def commit_check(self) -> ValidationResult:
        """Phase-1 + Phase-2 dry-run validation without applying config.

        Phase 1: Pydantic schema + cross-reference checks (always run).
        Phase 2: Kernel/FRR dry-run hook (calls self._phase2_check if overridden).
        Returns a ValidationResult; is_valid == True means safe to commit.
        """
        result = self._validator.validate(self.store.candidate)
        if result.is_valid:
            self._phase2_check(result)
        return result

    def _phase2_check(self, result: ValidationResult) -> None:
        """Override in subclasses or tests to add kernel/FRR dry-run checks."""

    # ------------------------------------------------------------------
    # Timer callback
    # ------------------------------------------------------------------

    def _auto_rollback(self) -> None:
        with self._timer_lock:
            self._confirmed_timer = None
        logger.warning("commit confirmed timeout — performing automatic rollback 0")
        try:
            self.rollback(0)
        except Exception as exc:
            logger.error("Auto-rollback failed: %s", exc)

    # ------------------------------------------------------------------
    # Properties
    # ------------------------------------------------------------------

    @property
    def pending_confirmed(self) -> bool:
        """True when a commit-confirmed timer is active."""
        with self._timer_lock:
            return self._confirmed_timer is not None
