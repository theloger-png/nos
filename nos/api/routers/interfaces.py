"""REST endpoints for interface configuration."""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field

from nos.api.auth import get_api_key
from nos.api.deps import get_store
from nos.config.store import ConfigStore

router = APIRouter(prefix="/interfaces", tags=["interfaces"])


# ---------------------------------------------------------------------------
# Request / Response models
# ---------------------------------------------------------------------------

class FamilyInetBody(BaseModel):
    address: dict[str, Any] = {}
    dhcp: bool = False


class FamilyInet6Body(BaseModel):
    address: dict[str, Any] = {}


class UnitBody(BaseModel):
    vlan_id: int | None = Field(None, ge=1, le=4094)
    family_inet: FamilyInetBody | None = None
    family_inet6: FamilyInet6Body | None = None
    family_ethernet_switching: dict[str, Any] | None = None


class InterfaceBody(BaseModel):
    description: str | None = None
    mtu: int | None = Field(None, ge=256, le=9192)
    speed: str | None = None
    duplex: str | None = None
    disable: bool | None = None
    family_inet: FamilyInetBody | None = None
    family_inet6: FamilyInet6Body | None = None
    unit: dict[str, UnitBody] | None = None


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

@router.get("", dependencies=[Depends(get_api_key)])
def list_interfaces(store: ConfigStore = Depends(get_store)) -> dict[str, Any]:
    """List all interfaces from running config."""
    running = store.get_running()
    return running.get("interfaces", {})


@router.get("/{name}", dependencies=[Depends(get_api_key)])
def get_interface(name: str, store: ConfigStore = Depends(get_store)) -> dict[str, Any]:
    """Get a single interface from running config."""
    running = store.get_running()
    interfaces = running.get("interfaces", {})
    if name not in interfaces:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"Interface {name!r} not found")
    return interfaces[name]


@router.post("/{name}", status_code=status.HTTP_200_OK, dependencies=[Depends(get_api_key)])
def set_interface(
    name: str,
    body: InterfaceBody,
    store: ConfigStore = Depends(get_store),
) -> dict[str, Any]:
    """Set interface config in candidate (does not auto-commit).

    Only non-None fields in the body are applied; existing keys are preserved
    unless explicitly overwritten.
    """
    candidate = store.get_candidate()
    interfaces = candidate.setdefault("interfaces", {})
    iface = interfaces.setdefault(name, {})

    patch = body.model_dump(exclude_none=True)
    _deep_merge(iface, patch)

    store.set_candidate(candidate)
    return iface


@router.delete("/{name}", status_code=status.HTTP_200_OK, dependencies=[Depends(get_api_key)])
def delete_interface(
    name: str,
    store: ConfigStore = Depends(get_store),
) -> dict[str, str]:
    """Delete an interface from candidate config (does not auto-commit)."""
    candidate = store.get_candidate()
    interfaces = candidate.get("interfaces", {})
    if name not in interfaces:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"Interface {name!r} not found")
    del interfaces[name]
    store.set_candidate(candidate)
    return {"deleted": name}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _deep_merge(base: dict, patch: dict) -> None:
    """Recursively merge *patch* into *base* in-place."""
    for key, value in patch.items():
        if isinstance(value, dict) and isinstance(base.get(key), dict):
            _deep_merge(base[key], value)
        else:
            base[key] = value
