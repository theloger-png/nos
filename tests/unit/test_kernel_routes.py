"""Unit tests for nos.drivers.kernel.routes.RouteDriver."""
import socket
from unittest.mock import MagicMock

import pytest

from nos.drivers.kernel.routes import RouteDriver, _TABLE_MAIN


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_driver(mock_ip=None):
    ip = mock_ip or MagicMock()
    factory = MagicMock()
    factory.return_value.__enter__ = MagicMock(return_value=ip)
    factory.return_value.__exit__ = MagicMock(return_value=False)
    return RouteDriver(iproute_factory=factory), ip


# ---------------------------------------------------------------------------
# apply_route — next-hop routes
# ---------------------------------------------------------------------------

def test_apply_route_nexthop():
    driver, ip = _make_driver()
    driver.apply_route("10.0.0.0/24", {"next_hop": "192.168.1.1"})
    ip.route.assert_called_once_with(
        "replace",
        dst="10.0.0.0/24",
        family=socket.AF_INET,
        gateway="192.168.1.1",
        table=_TABLE_MAIN,
    )


def test_apply_route_ipv6_nexthop():
    driver, ip = _make_driver()
    driver.apply_route("2001:db8::/32", {"next_hop": "2001:db8::1"})
    ip.route.assert_called_once_with(
        "replace",
        dst="2001:db8::/32",
        family=socket.AF_INET6,
        gateway="2001:db8::1",
        table=_TABLE_MAIN,
    )


def test_apply_route_normalises_host_bits():
    """10.0.0.5/24 should be treated as 10.0.0.0/24."""
    driver, ip = _make_driver()
    driver.apply_route("10.0.0.5/24", {"next_hop": "10.0.0.1"})
    call_args = ip.route.call_args
    assert call_args.kwargs["dst"] == "10.0.0.0/24"


# ---------------------------------------------------------------------------
# apply_route — blackhole / prohibit
# ---------------------------------------------------------------------------

def test_apply_route_discard_creates_blackhole():
    driver, ip = _make_driver()
    driver.apply_route("10.99.0.0/16", {"discard": True})
    ip.route.assert_called_once_with(
        "replace",
        dst="10.99.0.0/16",
        family=socket.AF_INET,
        type="blackhole",
        table=_TABLE_MAIN,
    )


def test_apply_route_reject_creates_prohibit():
    driver, ip = _make_driver()
    driver.apply_route("10.99.0.0/16", {"reject": True})
    ip.route.assert_called_once_with(
        "replace",
        dst="10.99.0.0/16",
        family=socket.AF_INET,
        type="prohibit",
        table=_TABLE_MAIN,
    )


def test_apply_route_raises_without_nexthop():
    driver, ip = _make_driver()
    with pytest.raises(ValueError, match="next_hop required"):
        driver.apply_route("10.0.0.0/8", {})


# ---------------------------------------------------------------------------
# apply_route — custom table (VRF)
# ---------------------------------------------------------------------------

def test_apply_vrf_route_uses_custom_table():
    driver, ip = _make_driver()
    driver.apply_vrf_route("10.0.0.0/24", {"next_hop": "10.1.0.1"}, vrf_table=1001)
    call_kwargs = ip.route.call_args.kwargs
    assert call_kwargs["table"] == 1001


# ---------------------------------------------------------------------------
# delete_route
# ---------------------------------------------------------------------------

def test_delete_route_sends_del():
    driver, ip = _make_driver()
    driver.delete_route("10.0.0.0/24")
    ip.route.assert_called_once_with(
        "del",
        dst="10.0.0.0/24",
        family=socket.AF_INET,
        table=_TABLE_MAIN,
    )


def test_delete_route_ignores_enoent():
    ip = MagicMock()
    ip.route.side_effect = Exception("ENOENT: No such process")

    factory = MagicMock()
    factory.return_value.__enter__ = MagicMock(return_value=ip)
    factory.return_value.__exit__ = MagicMock(return_value=False)
    driver = RouteDriver(iproute_factory=factory)

    # Should not raise.
    driver.delete_route("10.0.0.0/24")


def test_delete_route_propagates_other_errors():
    ip = MagicMock()
    ip.route.side_effect = RuntimeError("unexpected kernel error")

    factory = MagicMock()
    factory.return_value.__enter__ = MagicMock(return_value=ip)
    factory.return_value.__exit__ = MagicMock(return_value=False)
    driver = RouteDriver(iproute_factory=factory)

    with pytest.raises(RuntimeError):
        driver.delete_route("10.0.0.0/24")
