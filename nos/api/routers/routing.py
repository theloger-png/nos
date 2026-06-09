"""REST endpoints for routing configuration."""
from __future__ import annotations

import urllib.parse
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Path, status
from pydantic import BaseModel, field_validator

from nos.api.auth import get_api_key
from nos.api.deps import get_store
from nos.config.store import ConfigStore

router = APIRouter(prefix="/routes", tags=["routing"])


# ---------------------------------------------------------------------------
# Request / Response models
# ---------------------------------------------------------------------------

class StaticRouteBody(BaseModel):
    prefix: str
    next_hop: str | None = None
    discard: bool = False
    reject: bool = False

    @field_validator("prefix")
    @classmethod
    def validate_prefix(cls, v: str) -> str:
        import ipaddress
        try:
            ipaddress.ip_network(v, strict=False)
        except ValueError:
            raise ValueError(f"Invalid IP prefix: {v!r}")
        return v

    @field_validator("next_hop")
    @classmethod
    def validate_next_hop(cls, v: str | None) -> str | None:
        if v is None:
            return v
        import ipaddress
        try:
            ipaddress.ip_address(v)
        except ValueError:
            raise ValueError(f"Invalid next-hop IP: {v!r}")
        return v


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

@router.get("", dependencies=[Depends(get_api_key)])
def list_routes(store: ConfigStore = Depends(get_store)) -> dict[str, Any]:
    """Return static routes from running config."""
    running = store.get_running()
    return running.get("routing_options", {}).get("static", {}).get("route", {})


@router.post("/static", status_code=status.HTTP_201_CREATED, dependencies=[Depends(get_api_key)])
def add_static_route(
    body: StaticRouteBody,
    store: ConfigStore = Depends(get_store),
) -> dict[str, Any]:
    """Add a static route to candidate config (does not auto-commit)."""
    candidate = store.get_candidate()
    routing_options = candidate.setdefault("routing_options", {})
    static = routing_options.setdefault("static", {})
    routes = static.setdefault("route", {})

    entry: dict[str, Any] = {}
    if body.next_hop is not None:
        entry["next_hop"] = body.next_hop
    if body.discard:
        entry["discard"] = True
    if body.reject:
        entry["reject"] = True

    routes[body.prefix] = entry
    store.set_candidate(candidate)
    return {body.prefix: entry}


@router.delete(
    "/static/{prefix:path}",
    status_code=status.HTTP_200_OK,
    dependencies=[Depends(get_api_key)],
)
def delete_static_route(
    prefix: str = Path(..., description="URL-encoded IP prefix, e.g. 10.0.0.0%2F24"),
    store: ConfigStore = Depends(get_store),
) -> dict[str, str]:
    """Delete a static route from candidate config (does not auto-commit).

    The *prefix* path segment must be URL-encoded (``/`` → ``%2F``).
    """
    decoded_prefix = urllib.parse.unquote(prefix)

    candidate = store.get_candidate()
    routes = (
        candidate.get("routing_options", {})
        .get("static", {})
        .get("route", {})
    )

    if decoded_prefix not in routes:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Static route {decoded_prefix!r} not found",
        )

    del routes[decoded_prefix]
    store.set_candidate(candidate)
    return {"deleted": decoded_prefix}
