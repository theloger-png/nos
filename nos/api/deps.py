"""Shared FastAPI dependencies: ConfigStore and CommitEngine singletons."""
from __future__ import annotations

import logging
import os
from functools import lru_cache
from pathlib import Path

from nos.config.commit import CommitEngine
from nos.config.store import ConfigStore

logger = logging.getLogger(__name__)

_BASE_DIR = Path(os.environ.get("NOS_BASE_DIR", "/opt/nos"))


@lru_cache(maxsize=1)
def _get_store() -> ConfigStore:
    return ConfigStore(base_dir=_BASE_DIR)


@lru_cache(maxsize=1)
def _get_engine() -> CommitEngine:
    return CommitEngine(_get_store(), base_dir=_BASE_DIR)


def get_store() -> ConfigStore:
    """FastAPI dependency that returns the shared ConfigStore instance."""
    return _get_store()


def get_engine() -> CommitEngine:
    """FastAPI dependency that returns the shared CommitEngine instance."""
    return _get_engine()


def reset_singletons() -> None:
    """Clear cached singletons (used in tests)."""
    _get_store.cache_clear()
    _get_engine.cache_clear()
