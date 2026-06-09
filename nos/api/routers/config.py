"""REST endpoints for config commit, rollback, and compare operations."""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Path, status

from nos.api.auth import get_api_key
from nos.api.deps import get_engine, get_store
from nos.config.commit import CommitEngine, CommitError, RollbackError
from nos.config.diff import diff
from nos.config.store import ConfigStore

router = APIRouter(prefix="/config", tags=["config"])

_MAX_ROLLBACKS = 50


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

@router.post("/commit", status_code=status.HTTP_200_OK, dependencies=[Depends(get_api_key)])
def commit_config(engine: CommitEngine = Depends(get_engine)) -> dict[str, str]:
    """Commit candidate config to running.

    Runs Phase-1 validation; returns 400 if validation fails.
    """
    try:
        engine.commit()
    except CommitError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"errors": [str(e) for e in exc.errors]},
        ) from exc
    return {"status": "committed"}


@router.post(
    "/rollback/{n}",
    status_code=status.HTTP_200_OK,
    dependencies=[Depends(get_api_key)],
)
def rollback_config(
    n: int = Path(..., ge=0, lt=_MAX_ROLLBACKS, description="Rollback checkpoint index (0–49)"),
    engine: CommitEngine = Depends(get_engine),
) -> dict[str, Any]:
    """Load rollback checkpoint N into candidate config.

    Running config and system state are unchanged; call POST /config/commit
    afterwards to apply the rolled-back candidate.
    """
    try:
        engine.rollback(n)
    except RollbackError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    return {"status": "loaded", "rollback": n}


@router.get("/compare", dependencies=[Depends(get_api_key)])
def compare_config(store: ConfigStore = Depends(get_store)) -> dict[str, str]:
    """Return a JunOS-style diff between candidate and running configs."""
    delta = diff(store.get_running(), store.get_candidate())
    return {"diff": delta}
