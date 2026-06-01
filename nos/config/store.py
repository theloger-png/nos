from __future__ import annotations

import copy
import json
import logging
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger(__name__)

_RUNNING_FILE = Path("config") / "running.json"
_CANDIDATE_FILE = Path("config") / "candidate.json"


class ConfigStore:
    """Manages candidate and running configurations in memory and on disk.

    At startup the running config is loaded from ``config/running.json``
    (relative to *base_dir*) and the candidate is initialised as a deep copy
    of running.  All modifications go to candidate; ``commit()`` promotes
    candidate → running and persists both files.
    """

    def __init__(self, base_dir: Optional[Path] = None) -> None:
        self.base_dir: Path = (
            Path(base_dir)
            if base_dir is not None
            else Path(__file__).resolve().parent.parent.parent
        )
        self.running: dict = {}
        self.candidate: dict = {}
        self.load_running()

    # ------------------------------------------------------------------
    # File paths
    # ------------------------------------------------------------------

    @property
    def _running_path(self) -> Path:
        return self.base_dir / _RUNNING_FILE

    @property
    def _candidate_path(self) -> Path:
        return self.base_dir / _CANDIDATE_FILE

    # ------------------------------------------------------------------
    # Load / Save
    # ------------------------------------------------------------------

    def load_running(self) -> None:
        """Load running config from disk and reset candidate to match."""
        if self._running_path.exists():
            with open(self._running_path) as fh:
                self.running = json.load(fh)
            logger.debug("Loaded running config from %s", self._running_path)
        else:
            self.running = {}
            logger.debug("No running config found at %s; starting empty", self._running_path)
        self.candidate = copy.deepcopy(self.running)

    def save_running(self) -> None:
        """Persist the in-memory running config to disk."""
        self._running_path.parent.mkdir(parents=True, exist_ok=True)
        with open(self._running_path, "w") as fh:
            json.dump(self.running, fh, indent=2)
        logger.debug("Saved running config to %s", self._running_path)

    def save_candidate(self) -> None:
        """Persist the in-memory candidate config to disk."""
        self._candidate_path.parent.mkdir(parents=True, exist_ok=True)
        with open(self._candidate_path, "w") as fh:
            json.dump(self.candidate, fh, indent=2)
        logger.debug("Saved candidate config to %s", self._candidate_path)

    # ------------------------------------------------------------------
    # Accessors (always return deep copies to prevent accidental mutation)
    # ------------------------------------------------------------------

    def get_running(self) -> dict:
        return copy.deepcopy(self.running)

    def get_candidate(self) -> dict:
        return copy.deepcopy(self.candidate)

    def set_candidate(self, config: dict) -> None:
        self.candidate = copy.deepcopy(config)

    # ------------------------------------------------------------------
    # Candidate manipulation
    # ------------------------------------------------------------------

    def update_candidate(self, path: list[str], value: Any) -> None:
        """Set *value* at the dotted *path* inside the candidate config.

        Intermediate dict nodes are created as needed.
        """
        node = self.candidate
        for key in path[:-1]:
            node = node.setdefault(key, {})
        node[path[-1]] = value

    def delete_candidate(self, path: list[str]) -> None:
        """Remove the key at *path* from the candidate config (no-op if absent)."""
        node = self.candidate
        for key in path[:-1]:
            if key not in node or not isinstance(node[key], dict):
                return
            node = node[key]
        node.pop(path[-1], None)

    def discard(self) -> None:
        """Reset candidate to the current running config, discarding all changes."""
        self.candidate = copy.deepcopy(self.running)

    # ------------------------------------------------------------------
    # Commit
    # ------------------------------------------------------------------

    def commit(self) -> None:
        """Promote candidate to running and persist both configs to disk."""
        self.running = copy.deepcopy(self.candidate)
        self.save_running()
        self.save_candidate()
