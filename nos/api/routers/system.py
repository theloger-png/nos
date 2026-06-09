"""REST endpoints for system status and forwarding information."""
from __future__ import annotations

import time
from typing import Any

from fastapi import APIRouter, Depends

from nos.api.auth import get_api_key
from nos.api.deps import get_store
from nos.config.store import ConfigStore

router = APIRouter(prefix="/system", tags=["system"])

# NOS version string — updated as releases are tagged
_NOS_VERSION = "1.0.0-phase1"

# Process start time for uptime calculation
_START_TIME = time.monotonic()


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

@router.get("/status", dependencies=[Depends(get_api_key)])
def system_status(store: ConfigStore = Depends(get_store)) -> dict[str, Any]:
    """Return hostname, uptime in seconds, and NOS version."""
    running = store.get_running()
    hostname: str = running.get("system", {}).get("host_name", "nos")
    uptime_seconds = int(time.monotonic() - _START_TIME)
    return {
        "hostname": hostname,
        "uptime_seconds": uptime_seconds,
        "version": _NOS_VERSION,
    }


@router.get("/forwarding", dependencies=[Depends(get_api_key)])
def forwarding_status() -> dict[str, Any]:
    """Return XDP/kernel forwarding mode per interface.

    Reads live data from the PFE stats if available, otherwise returns
    an empty table (PFE process not running is non-fatal).
    """
    try:
        from nos.pfe.manager import PFEManager, ForwardingMode
        from pyroute2 import IPRoute

        modes: dict[str, str] = {}
        with IPRoute() as ipr:
            links = ipr.get_links()
        for link in links:
            ifname: str = link.get_attr("IFLA_IFNAME") or ""
            if not ifname:
                continue
            linkinfo = link.get_attr("IFLA_LINKINFO")
            xdp = link.get_attr("IFLA_XDP")
            if xdp is not None:
                attached = xdp.get_attr("IFLA_XDP_ATTACHED") if hasattr(xdp, "get_attr") else None
                if attached == 1:
                    modes[ifname] = ForwardingMode.XDP_NATIVE.value
                elif attached == 2:
                    modes[ifname] = ForwardingMode.XDP_GENERIC.value
                else:
                    modes[ifname] = ForwardingMode.KERNEL.value
            else:
                modes[ifname] = ForwardingMode.KERNEL.value
        return {"interfaces": modes}
    except Exception:
        return {"interfaces": {}}
