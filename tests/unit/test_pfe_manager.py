"""Unit tests for nos.pfe.manager.PFEManager."""
from contextlib import contextmanager
from unittest.mock import MagicMock, call, patch

import pytest

from nos.pfe.manager import ForwardingMode, PFEManager
from nos.pfe.ipc import PFEError
from nos.pfe.stats import StatsError


# ---------------------------------------------------------------------------
# fixtures / helpers
# ---------------------------------------------------------------------------

@contextmanager
def _make_manager():
    """Yield (PFEManager, mock_client, mock_fib, mock_stats, mock_writer) with
    all internal constructors patched so no real sockets or threads are created."""
    with patch("nos.pfe.manager.PFEClient") as MockClient, \
         patch("nos.pfe.manager.FIBManager") as MockFIB, \
         patch("nos.pfe.manager.StatsCollector") as MockStats, \
         patch("nos.pfe.manager.IfaceStatsWriter") as MockWriter:

        mock_client = MockClient.return_value
        mock_fib = MockFIB.return_value
        mock_stats = MockStats.return_value
        mock_writer = MockWriter.return_value
        # Default ping reply
        mock_client.send_message.return_value = {"status": "ok"}

        mgr = PFEManager()
        yield mgr, mock_client, mock_fib, mock_stats, mock_writer


@pytest.fixture
def manager_ctx():
    with _make_manager() as ctx:
        yield ctx


# Convenience unpacking
@pytest.fixture
def manager(manager_ctx):
    mgr, *_ = manager_ctx
    return mgr


@pytest.fixture
def mock_client(manager_ctx):
    _, client, *_ = manager_ctx
    return client


@pytest.fixture
def mock_stats(manager_ctx):
    _, _, _, stats, _ = manager_ctx
    return stats


@pytest.fixture
def mock_writer(manager_ctx):
    _, _, _, _, writer = manager_ctx
    return writer


# ---------------------------------------------------------------------------
# __init__ — object graph
# ---------------------------------------------------------------------------

class TestInit:
    def test_creates_pfe_client(self):
        with patch("nos.pfe.manager.PFEClient") as MockClient, \
             patch("nos.pfe.manager.FIBManager"), \
             patch("nos.pfe.manager.StatsCollector"), \
             patch("nos.pfe.manager.IfaceStatsWriter"):
            PFEManager()
            MockClient.assert_called_once_with()

    def test_injects_client_into_fib_manager(self):
        with patch("nos.pfe.manager.PFEClient") as MockClient, \
             patch("nos.pfe.manager.FIBManager") as MockFIB, \
             patch("nos.pfe.manager.StatsCollector"), \
             patch("nos.pfe.manager.IfaceStatsWriter"):
            PFEManager()
            MockFIB.assert_called_once_with(MockClient.return_value)

    def test_injects_client_into_stats_collector(self):
        with patch("nos.pfe.manager.PFEClient") as MockClient, \
             patch("nos.pfe.manager.FIBManager"), \
             patch("nos.pfe.manager.StatsCollector") as MockStats, \
             patch("nos.pfe.manager.IfaceStatsWriter"):
            PFEManager()
            MockStats.assert_called_once_with(MockClient.return_value)

    def test_not_available_initially(self, manager):
        assert not manager.is_available()


# ---------------------------------------------------------------------------
# properties
# ---------------------------------------------------------------------------

class TestProperties:
    def test_fib_returns_fib_manager(self, manager_ctx):
        mgr, _, mock_fib, *_ = manager_ctx
        assert mgr.fib is mock_fib

    def test_stats_returns_stats_collector(self, manager_ctx):
        mgr, _, _, mock_stats, _ = manager_ctx
        assert mgr.stats is mock_stats


# ---------------------------------------------------------------------------
# start()
# ---------------------------------------------------------------------------

class TestStart:
    def test_calls_connect(self, manager, mock_client):
        manager.start()
        mock_client.connect.assert_called_once()

    def test_sets_available_on_success(self, manager):
        manager.start()
        assert manager.is_available()

    def test_pfe_error_sets_unavailable(self, manager, mock_client):
        mock_client.connect.side_effect = PFEError("no socket")
        manager.start()
        assert not manager.is_available()

    def test_pfe_error_does_not_raise(self, manager, mock_client):
        mock_client.connect.side_effect = PFEError("no socket")
        manager.start()   # must not propagate

    def test_pfe_error_skips_stats_polling(self, manager, mock_client, mock_stats):
        mock_client.connect.side_effect = PFEError("no socket")
        manager.start(poll_ifindexes=[1, 2])
        mock_stats.start_polling.assert_not_called()

    def test_starts_stats_polling_when_ifindexes_given(
        self, manager, mock_stats
    ):
        manager.start(poll_ifindexes=[1, 2])
        mock_stats.start_polling.assert_called_once_with(
            [1, 2], interval=pytest.approx(30.0)
        )

    def test_no_stats_polling_without_ifindexes(self, manager, mock_stats):
        manager.start()
        mock_stats.start_polling.assert_not_called()

    def test_no_stats_polling_with_empty_list(self, manager, mock_stats):
        manager.start(poll_ifindexes=[])
        mock_stats.start_polling.assert_not_called()

    def test_stats_error_does_not_raise(self, manager, mock_stats):
        mock_stats.start_polling.side_effect = StatsError("already running")
        manager.start(poll_ifindexes=[1])   # must not propagate

    def test_available_even_when_stats_polling_fails(self, manager, mock_stats):
        mock_stats.start_polling.side_effect = StatsError("already running")
        manager.start(poll_ifindexes=[1])
        assert manager.is_available()

    def test_iface_stats_writer_started_unconditionally(self, manager, mock_writer):
        manager.start()
        mock_writer.start.assert_called_once()

    def test_iface_stats_writer_started_even_when_pfe_unavailable(
        self, manager, mock_client, mock_writer
    ):
        mock_client.connect.side_effect = PFEError("no socket")
        manager.start()
        mock_writer.start.assert_called_once()


# ---------------------------------------------------------------------------
# stop()
# ---------------------------------------------------------------------------

class TestStop:
    def test_calls_stop_polling(self, manager, mock_stats):
        manager.stop()
        mock_stats.stop_polling.assert_called_once()

    def test_calls_disconnect(self, manager, mock_client):
        manager.stop()
        mock_client.disconnect.assert_called_once()

    def test_sets_unavailable(self, manager):
        manager.start()
        assert manager.is_available()
        manager.stop()
        assert not manager.is_available()

    def test_safe_before_start(self, manager):
        manager.stop()   # must not raise

    def test_calls_iface_stats_writer_stop(self, manager, mock_writer):
        manager.stop()
        mock_writer.stop.assert_called_once()

    def test_stop_order_polling_before_disconnect(self, manager_ctx):
        """stop_polling must be called before disconnect."""
        mgr, mock_client, _, mock_stats, _ = manager_ctx
        call_order = []
        mock_stats.stop_polling.side_effect = lambda: call_order.append("stop_polling")
        mock_client.disconnect.side_effect = lambda: call_order.append("disconnect")
        mgr.stop()
        assert call_order == ["stop_polling", "disconnect"]


# ---------------------------------------------------------------------------
# is_available()
# ---------------------------------------------------------------------------

class TestIsAvailable:
    def test_false_before_start(self, manager):
        assert not manager.is_available()

    def test_true_after_successful_start(self, manager):
        manager.start()
        assert manager.is_available()

    def test_false_after_stop(self, manager):
        manager.start()
        manager.stop()
        assert not manager.is_available()

    def test_false_when_connect_fails(self, manager, mock_client):
        mock_client.connect.side_effect = PFEError("refused")
        manager.start()
        assert not manager.is_available()


# ---------------------------------------------------------------------------
# detect_forwarding_mode() helpers
# ---------------------------------------------------------------------------

_XDP_ATTACHED_DRV = "xdpdrv"
_XDP_ATTACHED_SKB = "xdpgeneric"
_XDP_ATTACHED_NONE = "none"


def _make_ip_mock(ifname_to_attached: dict):
    """Return a patch context for nos.pfe.manager.IPRoute.

    Maps ifname → XDP_ATTACHED string value, or None for no IFLA_XDP attribute.
    """
    def _link_get(op, **kw):
        attached = ifname_to_attached.get(kw.get("ifname", ""))
        link = MagicMock()
        if attached is None:
            link.get_attr.return_value = None
        else:
            xdp_attr = MagicMock()
            xdp_attr.get_attr.return_value = attached
            link.get_attr.return_value = xdp_attr
        return [link]

    mock_ip = MagicMock()
    mock_ip.link.side_effect = _link_get
    mock_cls = MagicMock()
    mock_cls.return_value.__enter__.return_value = mock_ip
    mock_cls.return_value.__exit__.return_value = False
    return patch("nos.pfe.manager.IPRoute", mock_cls)


# ---------------------------------------------------------------------------
# detect_forwarding_mode()
# ---------------------------------------------------------------------------

class TestDetectForwardingMode:
    def test_kernel_when_unavailable(self, manager):
        assert not manager.is_available()
        assert manager.detect_forwarding_mode("eth0") == ForwardingMode.KERNEL

    def test_kernel_when_ping_fails(self, manager_ctx):
        mgr, mock_client, *_ = manager_ctx
        mgr._available = True
        mock_client.send_message.side_effect = PFEError("broken")
        assert mgr.detect_forwarding_mode("eth0") == ForwardingMode.KERNEL

    def test_kernel_when_ping_returns_error(self, manager_ctx):
        mgr, mock_client, *_ = manager_ctx
        mgr._available = True
        mock_client.send_message.return_value = {"status": "error", "message": "oops"}
        assert mgr.detect_forwarding_mode("eth0") == ForwardingMode.KERNEL

    def test_kernel_when_no_ifla_xdp(self, manager_ctx):
        mgr, *_ = manager_ctx
        mgr._available = True
        with _make_ip_mock({"eth0": None}):
            assert mgr.detect_forwarding_mode("eth0") == ForwardingMode.KERNEL

    def test_kernel_when_xdp_attached_none(self, manager_ctx):
        mgr, *_ = manager_ctx
        mgr._available = True
        with _make_ip_mock({"eth0": _XDP_ATTACHED_NONE}):
            assert mgr.detect_forwarding_mode("eth0") == ForwardingMode.KERNEL

    def test_xdp_native_when_xdp_attached_drv(self, manager_ctx):
        mgr, *_ = manager_ctx
        mgr._available = True
        with _make_ip_mock({"eth0": _XDP_ATTACHED_DRV}):
            assert mgr.detect_forwarding_mode("eth0") == ForwardingMode.XDP_NATIVE

    def test_xdp_generic_when_xdp_attached_skb(self, manager_ctx):
        mgr, *_ = manager_ctx
        mgr._available = True
        with _make_ip_mock({"eth0": _XDP_ATTACHED_SKB}):
            assert mgr.detect_forwarding_mode("eth0") == ForwardingMode.XDP_GENERIC

    def test_kernel_when_iproute_raises(self, manager_ctx):
        mgr, *_ = manager_ctx
        mgr._available = True
        with patch("nos.pfe.manager.IPRoute") as MockIPRoute:
            MockIPRoute.return_value.__enter__.side_effect = OSError("netlink error")
            assert mgr.detect_forwarding_mode("eth0") == ForwardingMode.KERNEL

    def test_sends_ping_before_xdp_check(self, manager_ctx):
        mgr, mock_client, *_ = manager_ctx
        mgr._available = True
        with _make_ip_mock({"eth0": _XDP_ATTACHED_SKB}):
            mgr.detect_forwarding_mode("eth0")
        mock_client.send_message.assert_called_once_with({"type": "ping"})

    def test_per_interface_detection(self, manager_ctx):
        """Different interfaces can have different modes."""
        mgr, *_ = manager_ctx
        mgr._available = True
        with _make_ip_mock({
            "eth0": _XDP_ATTACHED_DRV,
            "eth1": _XDP_ATTACHED_SKB,
            "eth2": None,
        }):
            assert mgr.detect_forwarding_mode("eth0") == ForwardingMode.XDP_NATIVE
            assert mgr.detect_forwarding_mode("eth1") == ForwardingMode.XDP_GENERIC
            assert mgr.detect_forwarding_mode("eth2") == ForwardingMode.KERNEL


# ---------------------------------------------------------------------------
# ForwardingMode enum
# ---------------------------------------------------------------------------

class TestForwardingModeEnum:
    def test_values(self):
        assert ForwardingMode.XDP_NATIVE.value == "xdp-native"
        assert ForwardingMode.XDP_GENERIC.value == "xdp-generic"
        assert ForwardingMode.KERNEL.value == "kernel"

    def test_members(self):
        modes = {m.name for m in ForwardingMode}
        assert modes == {"XDP_NATIVE", "XDP_GENERIC", "KERNEL"}
