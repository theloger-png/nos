"""API key authentication for the NOS REST API."""
from __future__ import annotations

import logging
import os
import secrets
from pathlib import Path

from fastapi import HTTPException, Security, status
from fastapi.security.api_key import APIKeyHeader

logger = logging.getLogger(__name__)

_API_KEY_PATH = Path(os.environ.get("NOS_API_KEY_PATH", "/opt/nos/api_key"))
_API_KEY_HEADER = APIKeyHeader(name="X-API-Key", auto_error=False)

_cached_key: str | None = None


def _load_or_create_key() -> str:
    """Return the API key, creating it on first call if the file does not exist."""
    global _cached_key
    if _cached_key is not None:
        return _cached_key

    if _API_KEY_PATH.exists():
        key = _API_KEY_PATH.read_text().strip()
        logger.info("Loaded API key from %s", _API_KEY_PATH)
    else:
        key = secrets.token_hex(32)
        _API_KEY_PATH.parent.mkdir(parents=True, exist_ok=True)
        _API_KEY_PATH.write_text(key)
        _API_KEY_PATH.chmod(0o600)
        logger.info("Generated new API key and saved to %s", _API_KEY_PATH)

    _cached_key = key
    return key


def get_api_key(api_key_header: str | None = Security(_API_KEY_HEADER)) -> str:
    """FastAPI dependency that validates the X-API-Key header.

    Raises HTTP 401 if the header is missing or incorrect.
    """
    expected = _load_or_create_key()
    if api_key_header is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="X-API-Key header is required",
        )
    if not secrets.compare_digest(api_key_header, expected):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid API key",
        )
    return api_key_header


def reset_key_cache() -> None:
    """Clear the in-memory key cache (used in tests)."""
    global _cached_key
    _cached_key = None
