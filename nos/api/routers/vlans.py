"""REST endpoints for VLAN configuration."""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field

from nos.api.auth import get_api_key
from nos.api.deps import get_store
from nos.config.store import ConfigStore

router = APIRouter(prefix="/vlans", tags=["vlans"])


# ---------------------------------------------------------------------------
# Request / Response models
# ---------------------------------------------------------------------------

class VlanBody(BaseModel):
    name: str
    vlan_id: int = Field(..., ge=1, le=4094)
    description: str | None = None
    l3_interface: str | None = None


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

@router.get("", dependencies=[Depends(get_api_key)])
def list_vlans(store: ConfigStore = Depends(get_store)) -> dict[str, Any]:
    """List all VLANs from running config."""
    running = store.get_running()
    return running.get("vlans", {})


@router.post("", status_code=status.HTTP_201_CREATED, dependencies=[Depends(get_api_key)])
def create_vlan(
    body: VlanBody,
    store: ConfigStore = Depends(get_store),
) -> dict[str, Any]:
    """Add or update a VLAN in candidate config (does not auto-commit)."""
    candidate = store.get_candidate()
    vlans = candidate.setdefault("vlans", {})

    entry: dict[str, Any] = {"vlan_id": body.vlan_id}
    if body.description is not None:
        entry["description"] = body.description
    if body.l3_interface is not None:
        entry["l3_interface"] = body.l3_interface

    vlans[body.name] = entry
    store.set_candidate(candidate)
    return {body.name: entry}


@router.delete("/{vlan_id}", status_code=status.HTTP_200_OK, dependencies=[Depends(get_api_key)])
def delete_vlan(
    vlan_id: int,
    store: ConfigStore = Depends(get_store),
) -> dict[str, str]:
    """Delete a VLAN by vlan-id from candidate config (does not auto-commit).

    Searches for the first VLAN entry whose ``vlan_id`` field matches.
    """
    candidate = store.get_candidate()
    vlans = candidate.get("vlans", {})

    target_name: str | None = None
    for name, cfg in vlans.items():
        if cfg.get("vlan_id") == vlan_id:
            target_name = name
            break

    if target_name is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"VLAN with vlan-id {vlan_id} not found",
        )

    del vlans[target_name]
    store.set_candidate(candidate)
    return {"deleted": target_name}
