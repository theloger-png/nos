"""Unit tests for nos.drivers.frr.isis.ISISGenerator."""
import pytest

from nos.drivers.frr.isis import ISISGenerator, _router_id_to_net


# ---------------------------------------------------------------------------
# _router_id_to_net
# ---------------------------------------------------------------------------

def test_router_id_to_net_format():
    net = _router_id_to_net("1.1.1.1")
    # Must be a valid IS-IS NET: area.sysid.sel
    parts = net.split(".")
    assert parts[-1] == "00"                # selector
    assert net.startswith("49.")            # private area


def test_router_id_to_net_known_value():
    # 1.1.1.1 → 6-byte system id 000001010101
    net = _router_id_to_net("1.1.1.1", area="49.0001")
    assert net == "49.0001.0000.0101.0101.00"


def test_router_id_to_net_custom_area():
    net = _router_id_to_net("10.20.30.40", area="49.1234")
    assert net.startswith("49.1234.")


def test_router_id_to_net_different_ips_differ():
    assert _router_id_to_net("1.1.1.1") != _router_id_to_net("2.2.2.2")


# ---------------------------------------------------------------------------
# ISISGenerator.render_interface
# ---------------------------------------------------------------------------

@pytest.fixture()
def gen():
    return ISISGenerator()


def test_render_interface_basic(gen):
    lines = gen.render_interface("eth0", {})
    text = "\n".join(lines)
    assert "interface eth0" in text
    assert "ip router isis default" in text


def test_render_interface_point_to_point(gen):
    lines = gen.render_interface("eth0", {"point_to_point": True})
    text = "\n".join(lines)
    assert "isis network point-to-point" in text


def test_render_interface_no_ptp_when_false(gen):
    lines = gen.render_interface("eth0", {"point_to_point": False})
    text = "\n".join(lines)
    assert "point-to-point" not in text


def test_render_interface_hello_interval(gen):
    lines = gen.render_interface("eth0", {"hello_interval": 10})
    text = "\n".join(lines)
    assert "isis hello-interval 10" in text


def test_render_interface_hold_time(gen):
    lines = gen.render_interface("eth0", {"hold_time": 30})
    text = "\n".join(lines)
    assert "isis hold-time 30" in text


def test_render_interface_ends_with_bang(gen):
    lines = gen.render_interface("lo0", {})
    assert lines[-1] == "!"


# ---------------------------------------------------------------------------
# ISISGenerator.render_router
# ---------------------------------------------------------------------------

def test_render_router_includes_net(gen):
    lines = gen.render_router({}, router_id="1.1.1.1")
    text = "\n".join(lines)
    assert "net 49." in text


def test_render_router_no_net_without_router_id(gen):
    lines = gen.render_router({})
    text = "\n".join(lines)
    assert "net " not in text


def test_render_router_level2_only(gen):
    isis_cfg = {
        "interface": {
            "eth0": {"level_1_disable": True},
        }
    }
    lines = gen.render_router(isis_cfg, router_id="1.1.1.1")
    text = "\n".join(lines)
    assert "level-2-only" in text


def test_render_router_level1_only(gen):
    isis_cfg = {
        "interface": {
            "eth0": {"level_2_disable": True},
        }
    }
    lines = gen.render_router(isis_cfg, router_id="1.1.1.1")
    text = "\n".join(lines)
    assert "level-1-only" in text


def test_render_router_level2_only_global_level_disable(gen):
    isis_cfg = {"level_1": {"disable": True}}
    lines = gen.render_router(isis_cfg, router_id="1.1.1.1")
    assert "is-type level-2-only" in "\n".join(lines)


def test_render_router_level1_only_global_level_disable(gen):
    isis_cfg = {"level_2": {"disable": True}}
    lines = gen.render_router(isis_cfg, router_id="1.1.1.1")
    assert "is-type level-1-only" in "\n".join(lines)


def test_render_router_wide_metrics(gen):
    isis_cfg = {"level_2": {"wide_metrics_only": True}}
    lines = gen.render_router(isis_cfg, router_id="1.1.1.1")
    text = "\n".join(lines)
    assert "metric-style wide" in text


def test_render_router_ends_with_bang(gen):
    lines = gen.render_router({})
    assert lines[-1] == "!"
