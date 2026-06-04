"""Unit tests for IS-IS lo0 loopback passive handling."""
import pytest

from nos.drivers.frr.isis import ISISGenerator


@pytest.fixture()
def gen():
    return ISISGenerator()


def test_lo0_is_passive_automatically(gen):
    lines = gen.render_interface("lo0", {})
    text = "\n".join(lines)
    assert "isis passive" in text


def test_lo1_is_passive_automatically(gen):
    lines = gen.render_interface("lo1", {})
    text = "\n".join(lines)
    assert "isis passive" in text


def test_eth0_not_passive_by_default(gen):
    lines = gen.render_interface("eth0", {})
    text = "\n".join(lines)
    assert "isis passive" not in text


def test_explicit_passive_flag_on_non_loopback(gen):
    lines = gen.render_interface("eth0", {"passive": True})
    text = "\n".join(lines)
    assert "isis passive" in text


def test_lo0_no_point_to_point_by_default(gen):
    lines = gen.render_interface("lo0", {})
    text = "\n".join(lines)
    assert "isis network point-to-point" not in text


def test_lo0_renders_ip_router_isis(gen):
    lines = gen.render_interface("lo0", {})
    text = "\n".join(lines)
    assert "ip router isis default" in text


def test_render_interface_body_lo0_has_passive(gen):
    body = gen.render_interface_body("lo0", {})
    text = "\n".join(body)
    assert "isis passive" in text
    assert "interface lo0" not in text
    assert "!" not in text


def test_render_interface_body_eth0_no_passive(gen):
    body = gen.render_interface_body("eth0", {})
    text = "\n".join(body)
    assert "isis passive" not in text
    assert "ip router isis default" in text


def test_render_interface_ends_with_bang_lo0(gen):
    lines = gen.render_interface("lo0", {})
    assert lines[-1] == "!"


def test_render_interface_starts_with_interface_header(gen):
    lines = gen.render_interface("lo0", {})
    assert lines[0] == "interface lo0"
