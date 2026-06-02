"""Tests for 'show configuration' (operational) and 'show <section>' (configure)."""
from __future__ import annotations

from unittest.mock import MagicMock, Mock, patch

import pytest
from prompt_toolkit.completion import CompleteEvent
from prompt_toolkit.document import Document

from nos.cli.completer import NOSCompleter
from nos.cli.modes.configure import ConfigureMode
from nos.cli.modes.operational import OperationalMode
from nos.cli.parser import CLIMode
from nos.config.commit import CommitEngine
from nos.config.store import ConfigStore
from nos.config.validator import ConfigValidator


# ============================================================================
# Helpers for mocking pyroute2 kernel data
# ============================================================================

class _MockLink:
    """Minimal stand-in for pyroute2 ifinfmsg."""

    def __init__(self, name: str, index: int, flags: int, mtu: int, operstate: str) -> None:
        self._a = {"IFLA_IFNAME": name, "IFLA_MTU": mtu, "IFLA_OPERSTATE": operstate}
        self._i = {"flags": flags, "index": index}

    def get_attr(self, key: str):
        return self._a.get(key)

    def __getitem__(self, key: str):
        return self._i[key]


class _MockAddr:
    """Minimal stand-in for pyroute2 ifaddrmsg."""

    def __init__(self, index: int, address: str, prefixlen: int, family: int = 2) -> None:
        self._a = {"IFA_ADDRESS": address}
        self._i = {"index": index, "prefixlen": prefixlen, "family": family}

    def get_attr(self, key: str):
        return self._a.get(key)

    def __getitem__(self, key: str):
        return self._i[key]


def _make_iproute_mock(links: list, addrs: list):
    """Return a mock IPRoute class whose instances yield the given links/addrs."""
    instance = Mock()
    instance.__enter__ = Mock(return_value=instance)
    instance.__exit__ = Mock(return_value=False)
    instance.get_links.return_value = links
    instance.get_addr.return_value = addrs
    return Mock(return_value=instance)


# ============================================================================
# Fixtures
# ============================================================================

@pytest.fixture
def store(tmp_path):
    return ConfigStore(base_dir=tmp_path)


@pytest.fixture
def engine(store):
    return CommitEngine(store, validator=ConfigValidator())


@pytest.fixture
def oper(store):
    return OperationalMode(store)


@pytest.fixture
def conf(store, engine):
    return ConfigureMode(store, engine)


@pytest.fixture
def populated_store(store, engine):
    """A store with a representative running config committed."""
    cm = ConfigureMode(store, engine)
    cm.execute("set system host-name nos01")
    cm.execute("set system domain-name example.com")
    cm.execute("set interfaces eth0 description internet")
    cm.execute("set interfaces eth0 family inet address 10.0.0.1/30")
    cm.execute("set interfaces lo0 family inet address 1.1.1.1/32")
    cm.execute("set vlans vlan100 vlan-id 100")
    cm.execute("set vlans vlan100 description management")
    cm.execute("set routing-options router-id 1.1.1.1")
    cm.execute("set routing-options autonomous-system 65000")
    cm.execute("set routing-options static route 0.0.0.0/0 next-hop 10.0.0.2")
    cm.execute("set protocols bgp group IBGP type internal")
    cm.execute("set protocols bgp group IBGP local-address 1.1.1.1")
    cm.execute("set protocols isis interface eth0 point-to-point")
    engine.commit()
    return store


# ============================================================================
# show interfaces — operational mode (live kernel data via pyroute2)
# ============================================================================

_PATCH_IPROUTE = "nos.cli.modes.operational.IPRoute"


class TestShowInterfacesOperational:
    def test_basic_up_interface(self, oper):
        links = [_MockLink("eth0", 2, 0, 1500, "UP")]
        mock_ip = _make_iproute_mock(links, [])
        with patch(_PATCH_IPROUTE, mock_ip):
            out = oper.execute("show interfaces")
        assert "Physical interface: eth0" in out
        assert "Physical link is Up" in out
        assert "MTU: 1500" in out

    def test_down_state_shown(self, oper):
        links = [_MockLink("eth1", 3, 0, 9000, "DOWN")]
        mock_ip = _make_iproute_mock(links, [])
        with patch(_PATCH_IPROUTE, mock_ip):
            out = oper.execute("show interfaces")
        assert "Physical link is Down" in out

    def test_unknown_operstate_shown_as_down(self, oper):
        links = [_MockLink("eth0", 2, 0, 1500, "UNKNOWN")]
        mock_ip = _make_iproute_mock(links, [])
        with patch(_PATCH_IPROUTE, mock_ip):
            out = oper.execute("show interfaces")
        assert "Physical link is Down" in out

    def test_loopback_skipped_by_default(self, oper):
        lo = _MockLink("lo", 1, 0x8, 65536, "UNKNOWN")  # IFF_LOOPBACK = 0x8
        eth0 = _MockLink("eth0", 2, 0, 1500, "UP")
        mock_ip = _make_iproute_mock([lo, eth0], [])
        with patch(_PATCH_IPROUTE, mock_ip):
            out = oper.execute("show interfaces")
        assert "Physical interface: lo" not in out
        assert "Physical interface: eth0" in out

    def test_loopback_shown_when_requested(self, oper):
        lo = _MockLink("lo", 1, 0x8, 65536, "UNKNOWN")
        mock_ip = _make_iproute_mock([lo], [])
        with patch(_PATCH_IPROUTE, mock_ip):
            out = oper.execute("show interfaces lo")
        assert "Physical interface: lo" in out

    def test_config_description_merged(self, oper, engine):
        cm = ConfigureMode(oper.store, engine)
        cm.execute("set interfaces eth0 description uplink")
        engine.commit()

        links = [_MockLink("eth0", 2, 0, 1500, "UP")]
        mock_ip = _make_iproute_mock(links, [])
        with patch(_PATCH_IPROUTE, mock_ip):
            out = oper.execute("show interfaces")
        assert "Description: uplink" in out

    def test_config_mtu_overrides_kernel_mtu(self, oper, engine):
        cm = ConfigureMode(oper.store, engine)
        cm.execute("set interfaces eth0 mtu 9000")
        engine.commit()

        links = [_MockLink("eth0", 2, 0, 1500, "UP")]
        mock_ip = _make_iproute_mock(links, [])
        with patch(_PATCH_IPROUTE, mock_ip):
            out = oper.execute("show interfaces")
        assert "MTU: 9000" in out

    def test_kernel_mtu_used_when_no_config_mtu(self, oper):
        links = [_MockLink("eth0", 2, 0, 4000, "UP")]
        mock_ip = _make_iproute_mock(links, [])
        with patch(_PATCH_IPROUTE, mock_ip):
            out = oper.execute("show interfaces")
        assert "MTU: 4000" in out

    def test_ip_addresses_from_kernel(self, oper):
        links = [_MockLink("eth0", 2, 0, 1500, "UP")]
        addrs = [_MockAddr(2, "192.168.1.1", 24)]
        mock_ip = _make_iproute_mock(links, addrs)
        with patch(_PATCH_IPROUTE, mock_ip):
            out = oper.execute("show interfaces")
        assert "Inet  192.168.1.1/24" in out

    def test_ip_addresses_not_from_config(self, oper, engine):
        # Config has an IP but kernel reports none — kernel wins
        cm = ConfigureMode(oper.store, engine)
        cm.execute("set interfaces eth0 family inet address 10.0.0.1/30")
        engine.commit()

        links = [_MockLink("eth0", 2, 0, 1500, "UP")]
        mock_ip = _make_iproute_mock(links, [])  # no addrs in kernel
        with patch(_PATCH_IPROUTE, mock_ip):
            out = oper.execute("show interfaces")
        assert "10.0.0.1" not in out

    def test_non_inet_addrs_skipped(self, oper):
        links = [_MockLink("eth0", 2, 0, 1500, "UP")]
        addrs = [_MockAddr(2, "fe80::1", 64, family=10)]  # AF_INET6
        mock_ip = _make_iproute_mock(links, addrs)
        with patch(_PATCH_IPROUTE, mock_ip):
            out = oper.execute("show interfaces")
        assert "fe80" not in out

    def test_disabled_interface_shown_as_disabled(self, oper, engine):
        cm = ConfigureMode(oper.store, engine)
        cm.execute("set interfaces eth0 disable")
        engine.commit()

        links = [_MockLink("eth0", 2, 0, 1500, "DOWN")]
        mock_ip = _make_iproute_mock(links, [])
        with patch(_PATCH_IPROUTE, mock_ip):
            out = oper.execute("show interfaces")
        assert "Disabled" in out

    def test_kernel_interface_without_config_still_shown(self, oper):
        # Interface in kernel but not in config at all
        links = [_MockLink("eth99", 5, 0, 1500, "UP")]
        mock_ip = _make_iproute_mock(links, [])
        with patch(_PATCH_IPROUTE, mock_ip):
            out = oper.execute("show interfaces")
        assert "Physical interface: eth99" in out
        assert "Enabled" in out

    def test_no_interfaces_returns_no_interfaces_found(self, oper):
        mock_ip = _make_iproute_mock([], [])
        with patch(_PATCH_IPROUTE, mock_ip):
            out = oper.execute("show interfaces")
        assert "No interfaces found" in out

    def test_interfaces_sorted_alphabetically(self, oper):
        links = [
            _MockLink("eth1", 3, 0, 1500, "UP"),
            _MockLink("eth0", 2, 0, 1500, "UP"),
        ]
        mock_ip = _make_iproute_mock(links, [])
        with patch(_PATCH_IPROUTE, mock_ip):
            out = oper.execute("show interfaces")
        assert out.index("eth0") < out.index("eth1")

    def test_fallback_to_config_when_pyroute2_unavailable(self, oper, engine):
        cm = ConfigureMode(oper.store, engine)
        cm.execute("set interfaces eth0 description fallback")
        engine.commit()

        with patch(_PATCH_IPROUTE, None):
            out = oper.execute("show interfaces")
        assert "eth0" in out
        assert "fallback" in out
        assert "Unknown" in out  # config-only path uses "Unknown" link state

    def test_fallback_empty_config(self, oper):
        with patch(_PATCH_IPROUTE, None):
            out = oper.execute("show interfaces")
        assert "No interfaces configured" in out

    def test_kernel_error_falls_back_to_config(self, oper, engine):
        cm = ConfigureMode(oper.store, engine)
        cm.execute("set interfaces eth0 description fallback")
        engine.commit()

        broken_instance = Mock()
        broken_instance.__enter__ = Mock(return_value=broken_instance)
        broken_instance.__exit__ = Mock(return_value=False)
        broken_instance.get_links.side_effect = OSError("permission denied")
        broken_ip = Mock(return_value=broken_instance)

        with patch(_PATCH_IPROUTE, broken_ip):
            out = oper.execute("show interfaces")
        assert "eth0" in out
        assert "Unknown" in out


# ============================================================================
# show interfaces terse — operational mode
# ============================================================================

_TERSE_HDR = (
    f"{'Interface':<24}{'Admin':<6}{'Link':<5}"
    f"{'Proto':<9}{'Local':<22}Remote"
)
_DESC_HDR = f"{'Interface':<24}{'Admin':<6}{'Link':<5}Description"


class TestShowInterfacesTerse:
    def test_header_line(self, oper):
        mock_ip = _make_iproute_mock([], [])
        with patch(_PATCH_IPROUTE, mock_ip):
            out = oper.execute("show interfaces terse")
        # No interfaces → "No interfaces found."
        assert "No interfaces found" in out

    def test_header_present(self, oper):
        links = [_MockLink("eth0", 2, 0, 1500, "UP")]
        mock_ip = _make_iproute_mock(links, [])
        with patch(_PATCH_IPROUTE, mock_ip):
            out = oper.execute("show interfaces terse")
        assert out.splitlines()[0] == _TERSE_HDR

    def test_physical_row_no_address(self, oper):
        links = [_MockLink("eth0", 2, 0, 1500, "UP")]
        mock_ip = _make_iproute_mock(links, [])
        with patch(_PATCH_IPROUTE, mock_ip):
            out = oper.execute("show interfaces terse")
        lines = out.splitlines()
        assert lines[1] == f"{'eth0':<24}{'up':<6}up"

    def test_logical_unit_row_with_address(self, oper):
        links = [_MockLink("ens33", 2, 0, 1500, "UP")]
        addrs = [_MockAddr(2, "172.18.4.44", 29)]
        mock_ip = _make_iproute_mock(links, addrs)
        with patch(_PATCH_IPROUTE, mock_ip):
            out = oper.execute("show interfaces terse")
        lines = out.splitlines()
        assert lines[1] == f"{'ens33':<24}{'up':<6}up"
        assert lines[2] == f"{'ens33.0':<24}{'up':<6}{'up':<5}{'inet':<9}172.18.4.44/29"

    def test_multiple_ips_continuation_lines(self, oper):
        links = [_MockLink("eth0", 2, 0, 1500, "UP")]
        addrs = [
            _MockAddr(2, "10.0.0.1", 30),
            _MockAddr(2, "192.168.1.1", 24),
        ]
        mock_ip = _make_iproute_mock(links, addrs)
        with patch(_PATCH_IPROUTE, mock_ip):
            out = oper.execute("show interfaces terse")
        lines = out.splitlines()
        # Physical row
        assert lines[1] == f"{'eth0':<24}{'up':<6}up"
        # First IP on .0 row
        assert "eth0.0" in lines[2]
        assert "10.0.0.1/30" in lines[2]
        # Second IP on continuation line (44 leading spaces)
        assert lines[3] == f"{'':44}192.168.1.1/24"

    def test_down_link(self, oper):
        links = [_MockLink("ens34", 3, 0, 1500, "DOWN")]
        mock_ip = _make_iproute_mock(links, [])
        with patch(_PATCH_IPROUTE, mock_ip):
            out = oper.execute("show interfaces terse")
        assert "down" in out.splitlines()[1]

    def test_admin_down_when_disabled_in_config(self, oper, engine):
        cm = ConfigureMode(oper.store, engine)
        cm.execute("set interfaces eth0 disable")
        engine.commit()
        links = [_MockLink("eth0", 2, 0, 1500, "UP")]
        mock_ip = _make_iproute_mock(links, [])
        with patch(_PATCH_IPROUTE, mock_ip):
            out = oper.execute("show interfaces terse")
        row = out.splitlines()[1]
        assert row.startswith(f"{'eth0':<24}down")

    def test_loopback_skipped(self, oper):
        lo = _MockLink("lo", 1, 0x8, 65536, "UNKNOWN")
        eth0 = _MockLink("eth0", 2, 0, 1500, "UP")
        mock_ip = _make_iproute_mock([lo, eth0], [])
        with patch(_PATCH_IPROUTE, mock_ip):
            out = oper.execute("show interfaces terse")
        assert "lo " not in out
        assert "eth0" in out

    def test_two_interfaces_sorted(self, oper):
        links = [
            _MockLink("eth1", 3, 0, 1500, "DOWN"),
            _MockLink("eth0", 2, 0, 1500, "UP"),
        ]
        mock_ip = _make_iproute_mock(links, [])
        with patch(_PATCH_IPROUTE, mock_ip):
            out = oper.execute("show interfaces terse")
        lines = out.splitlines()
        assert "eth0" in lines[1]
        assert "eth1" in lines[2]

    def test_fallback_config_link_dash(self, oper, engine):
        cm = ConfigureMode(oper.store, engine)
        cm.execute("set interfaces eth0 description uplink")
        engine.commit()
        with patch(_PATCH_IPROUTE, None):
            out = oper.execute("show interfaces terse")
        assert _TERSE_HDR in out
        assert "eth0" in out
        assert "-" in out  # link state unknown in config-only path

    def test_fallback_empty_config(self, oper):
        with patch(_PATCH_IPROUTE, None):
            out = oper.execute("show interfaces terse")
        assert "No interfaces found" in out

    def test_kernel_error_fallback(self, oper, engine):
        cm = ConfigureMode(oper.store, engine)
        cm.execute("set interfaces eth0 description x")
        engine.commit()
        broken = Mock()
        broken.__enter__ = Mock(return_value=broken)
        broken.__exit__ = Mock(return_value=False)
        broken.get_links.side_effect = OSError("eperm")
        with patch(_PATCH_IPROUTE, Mock(return_value=broken)):
            out = oper.execute("show interfaces terse")
        assert "eth0" in out


# ============================================================================
# show interfaces description — operational mode
# ============================================================================

class TestShowInterfacesDescription:
    def test_no_interfaces(self, oper):
        mock_ip = _make_iproute_mock([], [])
        with patch(_PATCH_IPROUTE, mock_ip):
            out = oper.execute("show interfaces description")
        assert "No interfaces found" in out

    def test_header_line(self, oper):
        links = [_MockLink("ens33", 2, 0, 1500, "UP")]
        mock_ip = _make_iproute_mock(links, [])
        with patch(_PATCH_IPROUTE, mock_ip):
            out = oper.execute("show interfaces description")
        assert out.splitlines()[0] == _DESC_HDR

    def test_row_with_description(self, oper, engine):
        cm = ConfigureMode(oper.store, engine)
        cm.execute("set interfaces ens33 description internet")
        engine.commit()
        links = [_MockLink("ens33", 2, 0, 1500, "UP")]
        mock_ip = _make_iproute_mock(links, [])
        with patch(_PATCH_IPROUTE, mock_ip):
            out = oper.execute("show interfaces description")
        assert out.splitlines()[1] == (
            f"{'ens33':<24}{'up':<6}{'up':<5}internet"
        )

    def test_row_without_description_no_trailing_spaces(self, oper):
        links = [_MockLink("eth0", 2, 0, 1500, "UP")]
        mock_ip = _make_iproute_mock(links, [])
        with patch(_PATCH_IPROUTE, mock_ip):
            out = oper.execute("show interfaces description")
        row = out.splitlines()[1]
        assert not row.endswith(" ")
        assert row == f"{'eth0':<24}{'up':<6}up"

    def test_down_link_with_description(self, oper, engine):
        cm = ConfigureMode(oper.store, engine)
        cm.execute("set interfaces ens34 description down")
        engine.commit()
        links = [_MockLink("ens34", 3, 0, 1500, "DOWN")]
        mock_ip = _make_iproute_mock(links, [])
        with patch(_PATCH_IPROUTE, mock_ip):
            out = oper.execute("show interfaces description")
        assert out.splitlines()[1] == (
            f"{'ens34':<24}{'up':<6}{'down':<5}down"
        )

    def test_two_interfaces_exact_junos_format(self, oper, engine):
        cm = ConfigureMode(oper.store, engine)
        cm.execute("set interfaces ens33 description internet")
        cm.execute("set interfaces ens34 description down")
        engine.commit()
        links = [
            _MockLink("ens33", 2, 0, 1500, "UP"),
            _MockLink("ens34", 3, 0, 1500, "DOWN"),
        ]
        mock_ip = _make_iproute_mock(links, [])
        with patch(_PATCH_IPROUTE, mock_ip):
            out = oper.execute("show interfaces description")
        lines = out.splitlines()
        assert lines[0] == _DESC_HDR
        assert lines[1] == f"{'ens33':<24}{'up':<6}{'up':<5}internet"
        assert lines[2] == f"{'ens34':<24}{'up':<6}{'down':<5}down"

    def test_admin_down_when_disabled(self, oper, engine):
        cm = ConfigureMode(oper.store, engine)
        cm.execute("set interfaces eth0 disable")
        engine.commit()
        links = [_MockLink("eth0", 2, 0, 1500, "UP")]
        mock_ip = _make_iproute_mock(links, [])
        with patch(_PATCH_IPROUTE, mock_ip):
            out = oper.execute("show interfaces description")
        assert "down" in out.splitlines()[1]

    def test_loopback_skipped(self, oper):
        lo = _MockLink("lo", 1, 0x8, 65536, "UNKNOWN")
        eth0 = _MockLink("eth0", 2, 0, 1500, "UP")
        mock_ip = _make_iproute_mock([lo, eth0], [])
        with patch(_PATCH_IPROUTE, mock_ip):
            out = oper.execute("show interfaces description")
        assert "lo " not in out
        assert "eth0" in out

    def test_fallback_config_only(self, oper, engine):
        cm = ConfigureMode(oper.store, engine)
        cm.execute("set interfaces eth0 description fallback")
        engine.commit()
        with patch(_PATCH_IPROUTE, None):
            out = oper.execute("show interfaces description")
        assert _DESC_HDR in out
        assert "eth0" in out
        assert "fallback" in out
        assert "-" in out  # config-only link state

    def test_fallback_empty_config(self, oper):
        with patch(_PATCH_IPROUTE, None):
            out = oper.execute("show interfaces description")
        assert "No interfaces found" in out

    def test_kernel_error_fallback(self, oper, engine):
        cm = ConfigureMode(oper.store, engine)
        cm.execute("set interfaces eth0 description x")
        engine.commit()
        broken = Mock()
        broken.__enter__ = Mock(return_value=broken)
        broken.__exit__ = Mock(return_value=False)
        broken.get_links.side_effect = OSError("eperm")
        with patch(_PATCH_IPROUTE, Mock(return_value=broken)):
            out = oper.execute("show interfaces description")
        assert "eth0" in out


# ============================================================================
# show interfaces — tab completion (operational mode)
# ============================================================================

class TestShowInterfacesCompletion:
    def test_show_interfaces_space_offers_terse(self):
        kws = complete_oper("show interfaces ")
        assert "terse" in kws

    def test_show_interfaces_space_offers_description(self):
        kws = complete_oper("show interfaces ")
        assert "description" in kws

    def test_show_interfaces_partial_t_completes_terse(self):
        kws = complete_oper("show interfaces t")
        assert "terse" in kws
        assert "description" not in kws

    def test_show_interfaces_partial_d_completes_description(self):
        kws = complete_oper("show interfaces d")
        assert "description" in kws
        assert "terse" not in kws

    def test_show_interfaces_terse_space_no_completions(self):
        kws = complete_oper("show interfaces terse ")
        assert kws == []

    def test_show_interfaces_description_space_no_completions(self):
        kws = complete_oper("show interfaces description ")
        assert kws == []

    def test_show_space_still_offers_interfaces(self):
        kws = complete_oper("show ")
        assert "interfaces" in kws

    def test_show_interfaces_does_not_offer_config_sections(self):
        # "show interfaces " should NOT bleed config-tree completions
        kws = complete_oper("show interfaces ")
        assert "system" not in kws
        assert "routing-options" not in kws


# ============================================================================
# show configuration — handler (operational mode)
# ============================================================================

class TestShowConfigurationHandler:
    def test_empty_config_returns_empty_message(self, oper):
        out = oper.execute("show configuration")
        assert "empty" in out.lower()

    def test_full_config_tree_format(self, oper, populated_store):
        out = oper.execute("show configuration")
        # Tree format uses { } ; not "set " prefixes
        assert "{" in out
        assert ";" in out
        assert not out.startswith("set ")

    def test_full_config_contains_system(self, oper, populated_store):
        out = oper.execute("show configuration")
        assert "host-name" in out
        assert "nos01" in out

    def test_full_config_contains_interfaces(self, oper, populated_store):
        out = oper.execute("show configuration")
        assert "eth0" in out

    def test_full_config_contains_routing_options(self, oper, populated_store):
        out = oper.execute("show configuration")
        assert "router-id" in out

    def test_section_interfaces_tree_format(self, oper, populated_store):
        out = oper.execute("show configuration interfaces")
        assert "eth0" in out
        # Other sections must not appear
        assert "host-name" not in out
        assert "router-id" not in out

    def test_section_system_tree_format(self, oper, populated_store):
        out = oper.execute("show configuration system")
        assert "host-name" in out
        assert "nos01" in out
        assert "eth0" not in out

    def test_section_routing_options_tree_format(self, oper, populated_store):
        out = oper.execute("show configuration routing-options")
        assert "router-id" in out
        assert "host-name" not in out

    def test_section_vlans_tree_format(self, oper, populated_store):
        out = oper.execute("show configuration vlans")
        assert "vlan100" in out
        assert "vlan-id" in out
        assert "host-name" not in out

    def test_section_protocols_tree_format(self, oper, populated_store):
        out = oper.execute("show configuration protocols")
        assert "bgp" in out
        assert "IBGP" in out
        assert "host-name" not in out

    def test_subsection_protocols_bgp_tree_format(self, oper, populated_store):
        out = oper.execute("show configuration protocols bgp")
        assert "IBGP" in out
        assert "type" in out
        assert "isis" not in out

    def test_subsection_protocols_isis_tree_format(self, oper, populated_store):
        out = oper.execute("show configuration protocols isis")
        assert "eth0" in out
        assert "point-to-point" in out
        assert "bgp" not in out

    def test_nonexistent_section(self, oper, populated_store):
        out = oper.execute("show configuration firewall")
        assert "no configuration" in out.lower() or "empty" in out.lower()

    def test_full_config_sections_appear_as_blocks(self, oper, populated_store):
        out = oper.execute("show configuration")
        assert "system {" in out
        assert "interfaces {" in out

    def test_pipe_match_filters_lines(self, oper, populated_store):
        out = oper.execute("show configuration | match host-name")
        assert "host-name" in out
        lines = out.splitlines()
        assert all("host-name" in ln for ln in lines)

    def test_pipe_except_excludes_lines(self, oper, populated_store):
        out = oper.execute("show configuration | except system")
        # "system {" block opener is removed
        assert "system {" not in out

    def test_section_with_pipe(self, oper, populated_store):
        out = oper.execute("show configuration interfaces | match eth0")
        lines = out.splitlines()
        assert all("eth0" in ln for ln in lines)

    # ------------------------------------------------------------------
    # display set pipe
    # ------------------------------------------------------------------

    def test_display_set_full_config_is_set_commands(self, oper, populated_store):
        out = oper.execute("show configuration | display set")
        lines = out.splitlines()
        assert lines
        assert all(ln.startswith("set ") for ln in lines)

    def test_display_set_contains_system(self, oper, populated_store):
        out = oper.execute("show configuration | display set")
        assert "set system host-name" in out
        assert "nos01" in out

    def test_display_set_contains_interfaces(self, oper, populated_store):
        out = oper.execute("show configuration | display set")
        assert "set interfaces" in out

    def test_display_set_section_interfaces(self, oper, populated_store):
        out = oper.execute("show configuration interfaces | display set")
        lines = out.splitlines()
        assert lines
        assert all(ln.startswith("set interfaces") for ln in lines)
        assert not any(ln.startswith("set system") for ln in lines)

    def test_display_set_section_system(self, oper, populated_store):
        out = oper.execute("show configuration system | display set")
        lines = out.splitlines()
        assert lines
        assert all(ln.startswith("set system") for ln in lines)

    def test_display_set_section_protocols_bgp(self, oper, populated_store):
        out = oper.execute("show configuration protocols bgp | display set")
        lines = out.splitlines()
        assert lines
        assert all(ln.startswith("set protocols bgp") for ln in lines)
        assert not any("isis" in ln for ln in lines)

    def test_display_set_output_is_sorted(self, oper, populated_store):
        out = oper.execute("show configuration | display set")
        lines = out.splitlines()
        assert lines == sorted(lines)


# ============================================================================
# show <section> — handler (configure mode)
# ============================================================================

class TestShowSectionConfigureHandler:
    def _setup(self, conf, engine):
        """Populate candidate config."""
        conf.execute("set system host-name nos01")
        conf.execute("set interfaces eth0 description internet")
        conf.execute("set interfaces eth0 family inet address 10.0.0.1/30")
        conf.execute("set vlans vlan100 vlan-id 100")
        conf.execute("set routing-options router-id 1.1.1.1")
        conf.execute("set protocols bgp group IBGP type internal")
        conf.execute("set protocols bgp group IBGP local-address 1.1.1.1")

    def test_show_no_args_shows_full_candidate(self, conf, engine):
        self._setup(conf, engine)
        out = conf.execute("show")
        assert "interfaces" in out
        assert "system" in out

    def test_show_interfaces_section(self, conf, engine):
        self._setup(conf, engine)
        out = conf.execute("show interfaces")
        # Should contain interface config but NOT system or vlans at top level
        assert "eth0" in out
        assert "description" in out
        assert "host-name" not in out
        assert "vlan-id" not in out

    def test_show_system_section(self, conf, engine):
        self._setup(conf, engine)
        out = conf.execute("show system")
        assert "host-name" in out
        assert "nos01" in out
        assert "interfaces" not in out

    def test_show_vlans_section(self, conf, engine):
        self._setup(conf, engine)
        out = conf.execute("show vlans")
        assert "vlan100" in out
        assert "vlan-id" in out
        assert "host-name" not in out

    def test_show_protocols_section(self, conf, engine):
        self._setup(conf, engine)
        out = conf.execute("show protocols")
        assert "bgp" in out
        assert "IBGP" in out
        assert "host-name" not in out

    def test_show_protocols_bgp_subsection(self, conf, engine):
        self._setup(conf, engine)
        out = conf.execute("show protocols bgp")
        assert "IBGP" in out
        assert "type" in out
        assert "isis" not in out

    def test_show_routing_options_section(self, conf, engine):
        self._setup(conf, engine)
        out = conf.execute("show routing-options")
        assert "router-id" in out

    def test_show_interfaces_eth0_subsection(self, conf, engine):
        self._setup(conf, engine)
        out = conf.execute("show interfaces eth0")
        assert "description" in out
        assert "internet" in out
        assert "family" in out

    def test_show_nonexistent_section(self, conf, engine):
        self._setup(conf, engine)
        out = conf.execute("show firewall")
        assert "no configuration" in out.lower() or "empty" in out.lower()

    def test_show_section_respects_edit_path(self, conf, engine):
        """At (interfaces)#, 'show eth0' shows eth0 config."""
        self._setup(conf, engine)
        conf.edit_path = ["interfaces"]
        out = conf.execute("show eth0")
        assert "description" in out
        assert "internet" in out
        # Other top-level sections should not appear
        assert "host-name" not in out

    def test_show_no_args_respects_edit_path(self, conf, engine):
        """At (protocols bgp)#, bare 'show' shows BGP config."""
        self._setup(conf, engine)
        conf.edit_path = ["protocols", "bgp"]
        out = conf.execute("show")
        assert "IBGP" in out
        assert "type" in out
        # Should not show system or interfaces
        assert "host-name" not in out

    def test_show_compare_still_works(self, conf, engine):
        conf.execute("set system host-name nos01")
        engine.commit()
        conf.execute("set system host-name nos02")
        out = conf.execute("show | compare")
        assert "nos02" in out or "nos01" in out

    def test_show_section_pipe_match(self, conf, engine):
        self._setup(conf, engine)
        out = conf.execute("show interfaces | match eth0")
        assert "eth0" in out


# ============================================================================
# Tab completion — show configuration (operational mode)
# ============================================================================

def complete_oper(text: str, store=None) -> list[str]:
    c = NOSCompleter(mode=CLIMode.OPERATIONAL, edit_path=[], store=store)
    doc = Document(text, len(text))
    return [c.text for c in c.get_completions(doc, CompleteEvent())]


class TestShowConfigurationCompletion:
    def test_show_space_includes_configuration(self):
        kws = complete_oper("show ")
        assert "configuration" in kws

    def test_show_conf_partial_completes(self):
        kws = complete_oper("show conf")
        assert "configuration" in kws

    def test_show_configuration_space_shows_sections(self):
        kws = complete_oper("show configuration ")
        assert "system" in kws
        assert "interfaces" in kws
        assert "vlans" in kws
        assert "routing-options" in kws
        assert "protocols" in kws
        assert "policy-options" in kws

    def test_show_configuration_sys_partial(self):
        kws = complete_oper("show configuration sys")
        assert "system" in kws
        assert "interfaces" not in kws

    def test_show_configuration_protocols_space(self):
        kws = complete_oper("show configuration protocols ")
        assert "isis" in kws
        assert "bgp" in kws

    def test_show_configuration_protocols_bgp_space(self):
        kws = complete_oper("show configuration protocols bgp ")
        assert "group" in kws

    def test_show_configuration_routing_options_space(self):
        kws = complete_oper("show configuration routing-options ")
        assert "static" in kws
        assert "router-id" in kws
        assert "autonomous-system" in kws


# ============================================================================
# Tab completion — show <section> (configure mode)
# ============================================================================

def complete_conf(text: str, edit_path=None, store=None) -> list[str]:
    c = NOSCompleter(
        mode=CLIMode.CONFIGURE,
        edit_path=edit_path or [],
        store=store,
    )
    doc = Document(text, len(text))
    return [c.text for c in c.get_completions(doc, CompleteEvent())]


class TestShowSectionConfigureCompletion:
    def test_show_space_offers_sections(self):
        kws = complete_conf("show ")
        assert "system" in kws
        assert "interfaces" in kws
        assert "vlans" in kws
        assert "routing-options" in kws
        assert "protocols" in kws

    def test_show_space_offers_pipe(self):
        kws = complete_conf("show ")
        assert "|" in kws

    def test_show_sys_partial(self):
        kws = complete_conf("show sys")
        assert "system" in kws
        assert "interfaces" not in kws

    def test_show_interfaces_space(self):
        kws = complete_conf("show interfaces ")
        # Should offer dynamic hint for interface names
        assert any("<interface-name>" in k for k in kws)

    def test_show_protocols_space(self):
        kws = complete_conf("show protocols ")
        assert "isis" in kws
        assert "bgp" in kws

    def test_show_protocols_bgp_space(self):
        kws = complete_conf("show protocols bgp ")
        assert "group" in kws

    def test_show_pipe_offers_compare(self):
        kws = complete_conf("show | ")
        assert "compare" in kws

    def test_show_interfaces_pipe_offers_compare_path(self):
        kws = complete_conf("show interfaces | ")
        assert "compare" in kws

    def test_show_section_with_edit_path(self):
        """At (interfaces)#, 'show ' should offer interface children."""
        kws = complete_conf("show ", edit_path=["interfaces"])
        # Should show interface-level completions (description, family, etc.)
        assert "description" in kws or any("<interface-name>" in k for k in kws)

    def test_show_space_at_root_no_operational_cmds(self):
        """Operational show sub-commands (bgp, route) should not appear here."""
        kws = complete_conf("show ")
        # "bgp" and "route" are not top-level config sections
        assert "route" not in kws

    def test_show_routing_options_space(self):
        kws = complete_conf("show routing-options ")
        assert "static" in kws
        assert "router-id" in kws


# ============================================================================
# Tab completion — show configuration pipe (operational mode)
# ============================================================================

class TestShowConfigurationPipeCompletion:
    def test_pipe_offered_after_show_configuration_space(self):
        kws = complete_oper("show configuration ")
        assert "|" in kws

    def test_pipe_offered_after_show_configuration_section(self):
        kws = complete_oper("show configuration interfaces ")
        assert "|" in kws

    def test_pipe_verbs_offered_after_pipe(self):
        kws = complete_oper("show configuration | ")
        assert "display" in kws

    def test_standard_pipe_verbs_offered(self):
        kws = complete_oper("show configuration | ")
        assert "match" in kws
        assert "except" in kws
        assert "count" in kws

    def test_display_partial_completes(self):
        kws = complete_oper("show configuration | dis")
        assert "display" in kws
        assert "match" not in kws

    def test_set_offered_after_display(self):
        kws = complete_oper("show configuration | display ")
        assert "set" in kws

    def test_set_partial_completes_after_display(self):
        kws = complete_oper("show configuration | display s")
        assert "set" in kws

    def test_no_completions_after_display_set(self):
        kws = complete_oper("show configuration | display set ")
        assert "set" not in kws

    def test_pipe_offered_after_section_and_space(self):
        kws = complete_oper("show configuration protocols ")
        assert "|" in kws

    def test_display_offered_after_section_pipe(self):
        kws = complete_oper("show configuration interfaces | ")
        assert "display" in kws

    def test_set_offered_after_section_pipe_display(self):
        kws = complete_oper("show configuration interfaces | display ")
        assert "set" in kws


# ============================================================================
# show | display set — configure mode handler
# ============================================================================

class TestShowSectionConfigureDisplaySet:
    def _setup(self, conf):
        conf.execute("set system host-name nos01")
        conf.execute("set interfaces eth0 description internet")
        conf.execute("set interfaces eth0 family inet address 10.0.0.1/30")
        conf.execute("set vlans vlan100 vlan-id 100")
        conf.execute("set routing-options router-id 1.1.1.1")

    def test_display_set_full_config_is_set_commands(self, conf, engine):
        self._setup(conf)
        out = conf.execute("show | display set")
        lines = [ln for ln in out.splitlines() if ln]
        assert lines
        assert all(ln.startswith("set ") for ln in lines)

    def test_display_set_full_config_contains_all_sections(self, conf, engine):
        self._setup(conf)
        out = conf.execute("show | display set")
        assert "set system host-name" in out
        assert "set interfaces" in out
        assert "set vlans" in out
        assert "set routing-options" in out

    def test_display_set_interfaces_section_only(self, conf, engine):
        self._setup(conf)
        out = conf.execute("show interfaces | display set")
        lines = [ln for ln in out.splitlines() if ln]
        assert lines
        assert all(ln.startswith("set interfaces") for ln in lines)
        assert not any(ln.startswith("set system") for ln in lines)
        assert not any(ln.startswith("set vlans") for ln in lines)

    def test_display_set_system_section_only(self, conf, engine):
        self._setup(conf)
        out = conf.execute("show system | display set")
        lines = [ln for ln in out.splitlines() if ln]
        assert lines
        assert all(ln.startswith("set system") for ln in lines)

    def test_display_set_interfaces_contains_address(self, conf, engine):
        self._setup(conf)
        out = conf.execute("show interfaces | display set")
        assert "set interfaces eth0 family inet address 10.0.0.1/30" in out

    def test_display_set_with_edit_path(self, conf, engine):
        self._setup(conf)
        conf.edit_path = ["interfaces"]
        out = conf.execute("show | display set")
        lines = [ln for ln in out.splitlines() if ln]
        assert lines
        assert all(ln.startswith("set interfaces") for ln in lines)
        assert not any(ln.startswith("set system") for ln in lines)

    def test_display_set_output_sorted(self, conf, engine):
        self._setup(conf)
        out = conf.execute("show | display set")
        lines = [ln for ln in out.splitlines() if ln]
        assert lines == sorted(lines)

    def test_show_interfaces_except_description(self, conf, engine):
        self._setup(conf)
        out = conf.execute("show interfaces | except description")
        assert "description" not in out
        assert "eth0" in out

    def test_show_match_keeps_only_matching_lines(self, conf, engine):
        self._setup(conf)
        out = conf.execute("show | match host-name")
        assert "host-name" in out
        lines = [ln for ln in out.splitlines() if ln]
        assert all("host-name" in ln for ln in lines)


# ============================================================================
# Tab completion — show | pipe (configure mode)
# ============================================================================

class TestShowSectionConfigurePipeCompletion:
    def test_pipe_offers_display(self):
        kws = complete_conf("show | ")
        assert "display" in kws

    def test_pipe_offers_match(self):
        kws = complete_conf("show | ")
        assert "match" in kws

    def test_pipe_offers_except(self):
        kws = complete_conf("show | ")
        assert "except" in kws

    def test_pipe_offers_find(self):
        kws = complete_conf("show | ")
        assert "find" in kws

    def test_pipe_offers_count(self):
        kws = complete_conf("show | ")
        assert "count" in kws

    def test_pipe_offers_compare(self):
        kws = complete_conf("show | ")
        assert "compare" in kws

    def test_pipe_partial_display(self):
        kws = complete_conf("show | dis")
        assert "display" in kws
        assert "match" not in kws

    def test_display_set_completion(self):
        kws = complete_conf("show | display ")
        assert "set" in kws

    def test_display_set_partial(self):
        kws = complete_conf("show | display s")
        assert "set" in kws

    def test_pipe_after_section_offers_display(self):
        kws = complete_conf("show interfaces | ")
        assert "display" in kws
        assert "match" in kws

    def test_display_set_after_section(self):
        kws = complete_conf("show interfaces | display ")
        assert "set" in kws

    def test_compare_partial_completes(self):
        kws = complete_conf("show | comp")
        assert "compare" in kws


# ============================================================================
# show forwarding — operational mode
# ============================================================================

_FWD_HDR = f"{'Interface':<13}{'Mode':<14}Status"


def _make_pfe(available: bool = True, mode_map: dict | None = None):
    """Return a mock PFEManager.

    *mode_map* maps ifname → ForwardingMode; defaults to XDP_GENERIC for all.
    """
    from nos.pfe.manager import ForwardingMode
    pfe = MagicMock()
    pfe.is_available.return_value = available
    if mode_map is None:
        pfe.detect_forwarding_mode.return_value = ForwardingMode.XDP_GENERIC
    else:
        pfe.detect_forwarding_mode.side_effect = lambda name: mode_map[name]
    return pfe


class TestShowForwarding:
    def test_header_present(self, store):
        oper = OperationalMode(store)
        mock_ip = _make_iproute_mock([], [])
        with patch(_PATCH_IPROUTE, mock_ip):
            out = oper.execute("show forwarding")
        assert _FWD_HDR in out

    def test_no_interfaces_message(self, store):
        oper = OperationalMode(store)
        mock_ip = _make_iproute_mock([], [])
        with patch(_PATCH_IPROUTE, mock_ip):
            out = oper.execute("show forwarding")
        assert "no interfaces found" in out.lower()

    def test_loopback_skipped(self, store):
        oper = OperationalMode(store, pfe=_make_pfe())
        lo = _MockLink("lo", 1, 0x8, 65536, "UNKNOWN")
        eth0 = _MockLink("eth0", 2, 0, 1500, "UP")
        mock_ip = _make_iproute_mock([lo, eth0], [])
        with patch(_PATCH_IPROUTE, mock_ip):
            out = oper.execute("show forwarding")
        assert "lo" not in out
        assert "eth0" in out

    def test_up_interface_shows_active(self, store):
        oper = OperationalMode(store, pfe=_make_pfe())
        mock_ip = _make_iproute_mock([_MockLink("ens33", 2, 0, 1500, "UP")], [])
        with patch(_PATCH_IPROUTE, mock_ip):
            out = oper.execute("show forwarding")
        assert "active" in out
        assert "inactive" not in out

    def test_down_interface_shows_inactive(self, store):
        oper = OperationalMode(store, pfe=_make_pfe())
        mock_ip = _make_iproute_mock([_MockLink("ens34", 3, 0, 1500, "DOWN")], [])
        with patch(_PATCH_IPROUTE, mock_ip):
            out = oper.execute("show forwarding")
        assert "inactive" in out

    def test_unknown_operstate_shows_inactive(self, store):
        oper = OperationalMode(store, pfe=_make_pfe())
        mock_ip = _make_iproute_mock([_MockLink("eth0", 2, 0, 1500, "UNKNOWN")], [])
        with patch(_PATCH_IPROUTE, mock_ip):
            out = oper.execute("show forwarding")
        assert "inactive" in out

    def test_pfe_none_shows_kernel_mode(self, store):
        oper = OperationalMode(store, pfe=None)
        mock_ip = _make_iproute_mock([_MockLink("eth0", 2, 0, 1500, "UP")], [])
        with patch(_PATCH_IPROUTE, mock_ip):
            out = oper.execute("show forwarding")
        assert "kernel" in out

    def test_pfe_unavailable_shows_kernel_mode(self, store):
        oper = OperationalMode(store, pfe=_make_pfe(available=False))
        mock_ip = _make_iproute_mock([_MockLink("eth0", 2, 0, 1500, "UP")], [])
        with patch(_PATCH_IPROUTE, mock_ip):
            out = oper.execute("show forwarding")
        assert "kernel" in out

    def test_pfe_available_calls_detect_per_interface(self, store):
        from nos.pfe.manager import ForwardingMode
        pfe = _make_pfe(available=True)
        pfe.detect_forwarding_mode.return_value = ForwardingMode.XDP_NATIVE
        oper = OperationalMode(store, pfe=pfe)
        mock_ip = _make_iproute_mock([_MockLink("eth0", 2, 0, 1500, "UP")], [])
        with patch(_PATCH_IPROUTE, mock_ip):
            out = oper.execute("show forwarding")
        pfe.detect_forwarding_mode.assert_called_once_with("eth0")
        assert "xdp-native" in out

    def test_mixed_modes_per_interface(self, store):
        from nos.pfe.manager import ForwardingMode
        mode_map = {
            "eth0": ForwardingMode.XDP_NATIVE,
            "eth1": ForwardingMode.XDP_GENERIC,
            "eth2": ForwardingMode.KERNEL,
        }
        oper = OperationalMode(store, pfe=_make_pfe(available=True, mode_map=mode_map))
        links = [
            _MockLink("eth0", 2, 0, 1500, "UP"),
            _MockLink("eth1", 3, 0, 1500, "UP"),
            _MockLink("eth2", 4, 0, 1500, "DOWN"),
        ]
        mock_ip = _make_iproute_mock(links, [])
        with patch(_PATCH_IPROUTE, mock_ip):
            out = oper.execute("show forwarding")
        assert "xdp-native" in out
        assert "xdp-generic" in out
        assert "kernel" in out

    def test_interfaces_sorted_alphabetically(self, store):
        oper = OperationalMode(store, pfe=_make_pfe())
        links = [
            _MockLink("eth1", 3, 0, 1500, "UP"),
            _MockLink("eth0", 2, 0, 1500, "UP"),
        ]
        mock_ip = _make_iproute_mock(links, [])
        with patch(_PATCH_IPROUTE, mock_ip):
            out = oper.execute("show forwarding")
        assert out.index("eth0") < out.index("eth1")

    def test_pyroute2_unavailable_shows_message(self, store):
        oper = OperationalMode(store)
        with patch(_PATCH_IPROUTE, None):
            out = oper.execute("show forwarding")
        assert _FWD_HDR in out
        assert "unavailable" in out.lower()

    def test_junos_style_row_format(self, store):
        """Verify exact column alignment matches JunOS style."""
        from nos.pfe.manager import ForwardingMode
        pfe = _make_pfe(available=True)
        pfe.detect_forwarding_mode.return_value = ForwardingMode.XDP_GENERIC
        oper = OperationalMode(store, pfe=pfe)
        mock_ip = _make_iproute_mock([_MockLink("ens33", 2, 0, 1500, "UP")], [])
        with patch(_PATCH_IPROUTE, mock_ip):
            out = oper.execute("show forwarding")
        lines = out.splitlines()
        assert lines[0] == _FWD_HDR
        assert lines[1] == f"{'ens33':<13}{'xdp-generic':<14}active"


# ============================================================================
# show ethernet-switching table — helpers
# ============================================================================

_BR_IDX = 10   # fake bridge ifindex
_P1_IDX = 2    # fake port-1 (ens33) ifindex
_P2_IDX = 3    # fake port-2 (ens34) ifindex
_NUD_REACHABLE = 0x02
_NUD_PERMANENT = 0x80


class _MockFDBEntry:
    """Stand-in for a pyroute2 ndmsg bridge FDB entry."""

    def __init__(
        self,
        mac: str,
        master: int,
        port_ifindex: int,
        vlan_id: int | None = None,
        state: int = _NUD_REACHABLE,
    ) -> None:
        self._a: dict = {
            "NDA_LLADDR": mac,
            "NDA_MASTER": master,
            "NDA_VLAN": vlan_id,
        }
        self._i: dict = {"ifindex": port_ifindex, "state": state}

    def get_attr(self, key: str):
        return self._a.get(key)

    def __getitem__(self, key: str):
        return self._i[key]


def _make_fdb_iproute_mock(
    links: list,
    fdb_entries: list,
    br_ifname: str = "nos-br",
    br_idx: int = _BR_IDX,
) -> Mock:
    """Return an IPRoute mock class that handles FDB dump calls."""
    instance = Mock()
    instance.__enter__ = Mock(return_value=instance)
    instance.__exit__ = Mock(return_value=False)
    instance.get_links.return_value = links
    instance.get_addr.return_value = []
    instance.link_lookup.side_effect = (
        lambda ifname=None: [br_idx] if ifname == br_ifname else []
    )
    instance.fdb.return_value = fdb_entries
    return Mock(return_value=instance)


# ============================================================================
# show ethernet-switching table — handler
# ============================================================================

class TestShowEthernetSwitching:

    # ------------------------------------------------------------------
    # Basic output
    # ------------------------------------------------------------------

    def test_no_bridge_returns_empty_table(self, oper):
        # link_lookup returns [] → bridge absent → empty entry list
        instance = Mock()
        instance.__enter__ = Mock(return_value=instance)
        instance.__exit__ = Mock(return_value=False)
        instance.link_lookup.return_value = []
        mock_ip = Mock(return_value=instance)
        with patch(_PATCH_IPROUTE, mock_ip):
            out = oper.execute("show ethernet-switching table")
        assert "0 entries" in out

    def test_pyroute2_unavailable_returns_error(self, oper):
        with patch(_PATCH_IPROUTE, None):
            out = oper.execute("show ethernet-switching table")
        assert "error" in out.lower()

    def test_basic_table_output(self, oper):
        links = [
            _MockLink("ens33", _P1_IDX, 0, 1500, "UP"),
            _MockLink("ens34", _P2_IDX, 0, 1500, "UP"),
        ]
        fdb = [
            _MockFDBEntry("aa:bb:cc:dd:ee:ff", _BR_IDX, _P1_IDX, vlan_id=101),
            _MockFDBEntry("00:50:56:95:0b:2b", _BR_IDX, _P2_IDX, vlan_id=101),
        ]
        mock_ip = _make_fdb_iproute_mock(links, fdb)
        with patch(_PATCH_IPROUTE, mock_ip):
            out = oper.execute("show ethernet-switching table")
        assert "2 entries" in out
        assert "aa:bb:cc:dd:ee:ff" in out
        assert "00:50:56:95:0b:2b" in out
        assert "ens33" in out
        assert "ens34" in out

    def test_header_summary_line_format(self, oper):
        mock_ip = _make_fdb_iproute_mock([], [])
        with patch(_PATCH_IPROUTE, mock_ip):
            out = oper.execute("show ethernet-switching table")
        assert out.startswith("Ethernet switching table:")

    def test_column_header_present(self, oper):
        links = [_MockLink("ens33", _P1_IDX, 0, 1500, "UP")]
        fdb = [_MockFDBEntry("aa:bb:cc:dd:ee:ff", _BR_IDX, _P1_IDX, vlan_id=101)]
        mock_ip = _make_fdb_iproute_mock(links, fdb)
        with patch(_PATCH_IPROUTE, mock_ip):
            out = oper.execute("show ethernet-switching table")
        assert "VLAN" in out
        assert "MAC address" in out
        assert "Type" in out
        assert "Interfaces" in out

    def test_learn_type_shown(self, oper):
        links = [_MockLink("ens33", _P1_IDX, 0, 1500, "UP")]
        fdb = [_MockFDBEntry("aa:bb:cc:dd:ee:ff", _BR_IDX, _P1_IDX, vlan_id=101)]
        mock_ip = _make_fdb_iproute_mock(links, fdb)
        with patch(_PATCH_IPROUTE, mock_ip):
            out = oper.execute("show ethernet-switching table")
        assert "Learn" in out

    def test_no_entries_no_column_header(self, oper):
        mock_ip = _make_fdb_iproute_mock([], [])
        with patch(_PATCH_IPROUTE, mock_ip):
            out = oper.execute("show ethernet-switching table")
        assert "MAC address" not in out

    # ------------------------------------------------------------------
    # Filtering — multicast / all-zeros / permanent
    # ------------------------------------------------------------------

    def test_multicast_mac_filtered(self, oper):
        links = [_MockLink("ens33", _P1_IDX, 0, 1500, "UP")]
        fdb = [
            _MockFDBEntry("01:00:5e:00:00:01", _BR_IDX, _P1_IDX, vlan_id=101),  # multicast
            _MockFDBEntry("aa:bb:cc:dd:ee:ff", _BR_IDX, _P1_IDX, vlan_id=101),  # unicast
        ]
        mock_ip = _make_fdb_iproute_mock(links, fdb)
        with patch(_PATCH_IPROUTE, mock_ip):
            out = oper.execute("show ethernet-switching table")
        assert "01:00:5e" not in out
        assert "aa:bb:cc:dd:ee:ff" in out
        assert "1 entries" in out

    def test_all_zeros_mac_filtered(self, oper):
        links = [_MockLink("ens33", _P1_IDX, 0, 1500, "UP")]
        fdb = [
            _MockFDBEntry("00:00:00:00:00:00", _BR_IDX, _P1_IDX, vlan_id=101),
            _MockFDBEntry("aa:bb:cc:dd:ee:ff", _BR_IDX, _P1_IDX, vlan_id=101),
        ]
        mock_ip = _make_fdb_iproute_mock(links, fdb)
        with patch(_PATCH_IPROUTE, mock_ip):
            out = oper.execute("show ethernet-switching table")
        assert "00:00:00:00:00:00" not in out
        assert "aa:bb:cc:dd:ee:ff" in out

    def test_permanent_entries_filtered(self, oper):
        links = [_MockLink("ens33", _P1_IDX, 0, 1500, "UP")]
        fdb = [
            _MockFDBEntry("aa:bb:cc:00:00:01", _BR_IDX, _P1_IDX, vlan_id=101,
                          state=_NUD_PERMANENT),
            _MockFDBEntry("aa:bb:cc:dd:ee:ff", _BR_IDX, _P1_IDX, vlan_id=101,
                          state=_NUD_REACHABLE),
        ]
        mock_ip = _make_fdb_iproute_mock(links, fdb)
        with patch(_PATCH_IPROUTE, mock_ip):
            out = oper.execute("show ethernet-switching table")
        assert "aa:bb:cc:00:00:01" not in out
        assert "aa:bb:cc:dd:ee:ff" in out

    def test_entries_from_other_bridges_filtered(self, oper):
        links = [_MockLink("ens33", _P1_IDX, 0, 1500, "UP")]
        other_br_idx = 99
        fdb = [
            _MockFDBEntry("aa:bb:cc:dd:ee:ff", other_br_idx, _P1_IDX, vlan_id=101),
        ]
        mock_ip = _make_fdb_iproute_mock(links, fdb)
        with patch(_PATCH_IPROUTE, mock_ip):
            out = oper.execute("show ethernet-switching table")
        assert "aa:bb:cc:dd:ee:ff" not in out
        assert "0 entries" in out

    # ------------------------------------------------------------------
    # VLAN name mapping
    # ------------------------------------------------------------------

    def test_vlan_id_mapped_to_config_name(self, oper, engine):
        cm = ConfigureMode(oper.store, engine)
        cm.execute("set vlans vlan101 vlan-id 101")
        engine.commit()

        links = [_MockLink("ens33", _P1_IDX, 0, 1500, "UP")]
        fdb = [_MockFDBEntry("aa:bb:cc:dd:ee:ff", _BR_IDX, _P1_IDX, vlan_id=101)]
        mock_ip = _make_fdb_iproute_mock(links, fdb)
        with patch(_PATCH_IPROUTE, mock_ip):
            out = oper.execute("show ethernet-switching table")
        assert "vlan101" in out

    def test_unknown_vlan_id_shown_as_vlanN(self, oper):
        links = [_MockLink("ens33", _P1_IDX, 0, 1500, "UP")]
        fdb = [_MockFDBEntry("aa:bb:cc:dd:ee:ff", _BR_IDX, _P1_IDX, vlan_id=999)]
        mock_ip = _make_fdb_iproute_mock(links, fdb)
        with patch(_PATCH_IPROUTE, mock_ip):
            out = oper.execute("show ethernet-switching table")
        assert "vlan999" in out

    def test_no_vlan_shown_as_default(self, oper):
        links = [_MockLink("ens33", _P1_IDX, 0, 1500, "UP")]
        fdb = [_MockFDBEntry("aa:bb:cc:dd:ee:ff", _BR_IDX, _P1_IDX, vlan_id=None)]
        mock_ip = _make_fdb_iproute_mock(links, fdb)
        with patch(_PATCH_IPROUTE, mock_ip):
            out = oper.execute("show ethernet-switching table")
        assert "default" in out

    # ------------------------------------------------------------------
    # Filter by interface
    # ------------------------------------------------------------------

    def test_filter_interface_keeps_matching(self, oper):
        links = [
            _MockLink("ens33", _P1_IDX, 0, 1500, "UP"),
            _MockLink("ens34", _P2_IDX, 0, 1500, "UP"),
        ]
        fdb = [
            _MockFDBEntry("aa:bb:cc:dd:ee:ff", _BR_IDX, _P1_IDX, vlan_id=101),
            _MockFDBEntry("00:50:56:95:0b:2b", _BR_IDX, _P2_IDX, vlan_id=101),
        ]
        mock_ip = _make_fdb_iproute_mock(links, fdb)
        with patch(_PATCH_IPROUTE, mock_ip):
            out = oper.execute("show ethernet-switching table interface ens33")
        assert "aa:bb:cc:dd:ee:ff" in out
        assert "00:50:56:95:0b:2b" not in out
        assert "1 entries" in out

    def test_filter_interface_no_match(self, oper):
        links = [_MockLink("ens33", _P1_IDX, 0, 1500, "UP")]
        fdb = [_MockFDBEntry("aa:bb:cc:dd:ee:ff", _BR_IDX, _P1_IDX, vlan_id=101)]
        mock_ip = _make_fdb_iproute_mock(links, fdb)
        with patch(_PATCH_IPROUTE, mock_ip):
            out = oper.execute("show ethernet-switching table interface ens99")
        assert "0 entries" in out

    # ------------------------------------------------------------------
    # Filter by VLAN
    # ------------------------------------------------------------------

    def test_filter_vlan_by_numeric_id(self, oper):
        links = [
            _MockLink("ens33", _P1_IDX, 0, 1500, "UP"),
            _MockLink("ens34", _P2_IDX, 0, 1500, "UP"),
        ]
        fdb = [
            _MockFDBEntry("aa:bb:cc:dd:ee:ff", _BR_IDX, _P1_IDX, vlan_id=101),
            _MockFDBEntry("00:50:56:95:0b:2b", _BR_IDX, _P2_IDX, vlan_id=200),
        ]
        mock_ip = _make_fdb_iproute_mock(links, fdb)
        with patch(_PATCH_IPROUTE, mock_ip):
            out = oper.execute("show ethernet-switching table vlan 101")
        assert "aa:bb:cc:dd:ee:ff" in out
        assert "00:50:56:95:0b:2b" not in out

    def test_filter_vlan_by_vlan_prefix(self, oper):
        links = [_MockLink("ens33", _P1_IDX, 0, 1500, "UP")]
        fdb = [_MockFDBEntry("aa:bb:cc:dd:ee:ff", _BR_IDX, _P1_IDX, vlan_id=101)]
        mock_ip = _make_fdb_iproute_mock(links, fdb)
        with patch(_PATCH_IPROUTE, mock_ip):
            out = oper.execute("show ethernet-switching table vlan vlan101")
        assert "aa:bb:cc:dd:ee:ff" in out

    def test_filter_vlan_by_config_name(self, oper, engine):
        cm = ConfigureMode(oper.store, engine)
        cm.execute("set vlans corp vlan-id 101")
        engine.commit()

        links = [
            _MockLink("ens33", _P1_IDX, 0, 1500, "UP"),
            _MockLink("ens34", _P2_IDX, 0, 1500, "UP"),
        ]
        fdb = [
            _MockFDBEntry("aa:bb:cc:dd:ee:ff", _BR_IDX, _P1_IDX, vlan_id=101),
            _MockFDBEntry("00:50:56:95:0b:2b", _BR_IDX, _P2_IDX, vlan_id=200),
        ]
        mock_ip = _make_fdb_iproute_mock(links, fdb)
        with patch(_PATCH_IPROUTE, mock_ip):
            out = oper.execute("show ethernet-switching table vlan corp")
        assert "aa:bb:cc:dd:ee:ff" in out
        assert "00:50:56:95:0b:2b" not in out

    def test_filter_vlan_unknown_returns_error(self, oper):
        mock_ip = _make_fdb_iproute_mock([], [])
        with patch(_PATCH_IPROUTE, mock_ip):
            out = oper.execute("show ethernet-switching table vlan nonexistent")
        assert "error" in out.lower()

    # ------------------------------------------------------------------
    # Summary
    # ------------------------------------------------------------------

    def test_summary_no_individual_macs(self, oper):
        links = [
            _MockLink("ens33", _P1_IDX, 0, 1500, "UP"),
            _MockLink("ens34", _P2_IDX, 0, 1500, "UP"),
        ]
        fdb = [
            _MockFDBEntry("aa:bb:cc:dd:ee:ff", _BR_IDX, _P1_IDX, vlan_id=101),
            _MockFDBEntry("00:50:56:95:0b:2b", _BR_IDX, _P2_IDX, vlan_id=101),
        ]
        mock_ip = _make_fdb_iproute_mock(links, fdb)
        with patch(_PATCH_IPROUTE, mock_ip):
            out = oper.execute("show ethernet-switching table summary")
        assert "aa:bb:cc" not in out
        assert "2 entries" in out

    def test_summary_counts_per_vlan(self, oper):
        links = [_MockLink("ens33", _P1_IDX, 0, 1500, "UP")]
        fdb = [
            _MockFDBEntry("aa:bb:cc:dd:ee:ff", _BR_IDX, _P1_IDX, vlan_id=101),
            _MockFDBEntry("aa:bb:cc:dd:ee:00", _BR_IDX, _P1_IDX, vlan_id=101),
        ]
        mock_ip = _make_fdb_iproute_mock(links, fdb)
        with patch(_PATCH_IPROUTE, mock_ip):
            out = oper.execute("show ethernet-switching table summary")
        assert "2" in out  # count
        assert "VLAN" in out

    def test_summary_counts_per_interface(self, oper):
        links = [
            _MockLink("ens33", _P1_IDX, 0, 1500, "UP"),
            _MockLink("ens34", _P2_IDX, 0, 1500, "UP"),
        ]
        fdb = [
            _MockFDBEntry("aa:bb:cc:dd:ee:ff", _BR_IDX, _P1_IDX, vlan_id=101),
            _MockFDBEntry("00:50:56:95:0b:2b", _BR_IDX, _P2_IDX, vlan_id=101),
            _MockFDBEntry("00:50:56:95:0b:2c", _BR_IDX, _P2_IDX, vlan_id=101),
        ]
        mock_ip = _make_fdb_iproute_mock(links, fdb)
        with patch(_PATCH_IPROUTE, mock_ip):
            out = oper.execute("show ethernet-switching table summary")
        assert "ens33" in out
        assert "ens34" in out
        assert "Interface" in out

    def test_summary_empty_table(self, oper):
        mock_ip = _make_fdb_iproute_mock([], [])
        with patch(_PATCH_IPROUTE, mock_ip):
            out = oper.execute("show ethernet-switching table summary")
        assert "0 entries" in out

    # ------------------------------------------------------------------
    # Error paths
    # ------------------------------------------------------------------

    def test_missing_table_keyword_shows_help(self, oper):
        mock_ip = _make_fdb_iproute_mock([], [])
        with patch(_PATCH_IPROUTE, mock_ip):
            out = oper.execute("show ethernet-switching")
        assert "table" in out.lower()

    def test_unknown_sub_option_returns_error(self, oper):
        mock_ip = _make_fdb_iproute_mock([], [])
        with patch(_PATCH_IPROUTE, mock_ip):
            out = oper.execute("show ethernet-switching table bogus")
        assert "error" in out.lower()

    def test_interface_without_arg_returns_error(self, oper):
        mock_ip = _make_fdb_iproute_mock([], [])
        with patch(_PATCH_IPROUTE, mock_ip):
            out = oper.execute("show ethernet-switching table interface")
        assert "error" in out.lower()

    def test_vlan_without_arg_returns_error(self, oper):
        mock_ip = _make_fdb_iproute_mock([], [])
        with patch(_PATCH_IPROUTE, mock_ip):
            out = oper.execute("show ethernet-switching table vlan")
        assert "error" in out.lower()

    def test_kernel_error_returns_error_message(self, oper):
        broken = Mock()
        broken.__enter__ = Mock(return_value=broken)
        broken.__exit__ = Mock(return_value=False)
        broken.link_lookup.side_effect = OSError("eperm")
        with patch(_PATCH_IPROUTE, Mock(return_value=broken)):
            out = oper.execute("show ethernet-switching table")
        assert "error" in out.lower()

    # ------------------------------------------------------------------
    # Column alignment
    # ------------------------------------------------------------------

    def test_exact_column_format(self, oper):
        links = [_MockLink("ens33", _P1_IDX, 0, 1500, "UP")]
        fdb = [_MockFDBEntry("aa:bb:cc:dd:ee:ff", _BR_IDX, _P1_IDX, vlan_id=101)]
        mock_ip = _make_fdb_iproute_mock(links, fdb)
        with patch(_PATCH_IPROUTE, mock_ip):
            out = oper.execute("show ethernet-switching table")
        data_line = [ln for ln in out.splitlines() if "aa:bb:cc" in ln][0]
        assert data_line == f"{'vlan101':<12}{'aa:bb:cc:dd:ee:ff':<19}{'Learn':<10}{'0':<5}ens33"


# ============================================================================
# show ethernet-switching — tab completion
# ============================================================================

class TestShowEthernetSwitchingCompletion:
    def test_show_space_offers_ethernet_switching(self):
        kws = complete_oper("show ")
        assert "ethernet-switching" in kws

    def test_show_ethernet_partial_completes(self):
        kws = complete_oper("show eth")
        assert "ethernet-switching" in kws

    def test_show_ethernet_switching_space_offers_table(self):
        kws = complete_oper("show ethernet-switching ")
        assert "table" in kws

    def test_show_ethernet_switching_table_partial(self):
        kws = complete_oper("show ethernet-switching tab")
        assert "table" in kws

    def test_show_ethernet_switching_table_space_offers_sub_cmds(self):
        kws = complete_oper("show ethernet-switching table ")
        assert "interface" in kws
        assert "vlan" in kws
        assert "summary" in kws

    def test_show_ethernet_switching_table_partial_i(self):
        kws = complete_oper("show ethernet-switching table i")
        assert "interface" in kws
        assert "vlan" not in kws
        assert "summary" not in kws

    def test_show_ethernet_switching_table_partial_v(self):
        kws = complete_oper("show ethernet-switching table v")
        assert "vlan" in kws
        assert "interface" not in kws

    def test_show_ethernet_switching_table_partial_s(self):
        kws = complete_oper("show ethernet-switching table s")
        assert "summary" in kws
        assert "interface" not in kws

    def test_show_ethernet_switching_table_interface_space_offers_hint(self):
        kws = complete_oper("show ethernet-switching table interface ")
        assert any("<interface-name>" in k for k in kws)

    def test_show_ethernet_switching_table_vlan_space_offers_hint(self):
        kws = complete_oper("show ethernet-switching table vlan ")
        assert any("<vlan-name-or-id>" in k for k in kws)


# ============================================================================
# show arp — helpers
# ============================================================================

_NUD_REACHABLE_T = 0x02
_NUD_STALE_T     = 0x04
_NUD_DELAY_T     = 0x08
_NUD_PROBE_T     = 0x10
_NUD_PERMANENT_T = 0x80
_NUD_INCOMPLETE  = 0x01
_NUD_FAILED      = 0x20
_NUD_NOARP       = 0x40


class _MockNeighbour:
    """Stand-in for a pyroute2 ndmsg ARP/neighbour entry."""

    def __init__(
        self,
        ip: str,
        mac: str,
        ifindex: int,
        state: int = _NUD_REACHABLE_T,
        family: int = 2,
    ) -> None:
        self._a: dict = {"NDA_DST": ip, "NDA_LLADDR": mac}
        self._i: dict = {"ifindex": ifindex, "state": state, "family": family}

    def get_attr(self, key: str):
        return self._a.get(key)

    def __getitem__(self, key: str):
        return self._i[key]


def _make_arp_iproute_mock(links: list, neighbours: list) -> Mock:
    """Return an IPRoute mock that supports get_links and get_neighbours."""
    instance = Mock()
    instance.__enter__ = Mock(return_value=instance)
    instance.__exit__ = Mock(return_value=False)
    instance.get_links.return_value = links
    instance.get_neighbours.return_value = neighbours
    return Mock(return_value=instance)


# ============================================================================
# show arp — handler
# ============================================================================

class TestShowArp:

    # ------------------------------------------------------------------
    # Basic output
    # ------------------------------------------------------------------

    def test_header_present(self, oper):
        mock_ip = _make_arp_iproute_mock([], [])
        with patch(_PATCH_IPROUTE, mock_ip):
            out = oper.execute("show arp")
        assert "MAC Address" in out
        assert "Address" in out
        assert "Interface" in out
        assert "Flags" in out

    def test_total_line_present(self, oper):
        mock_ip = _make_arp_iproute_mock([], [])
        with patch(_PATCH_IPROUTE, mock_ip):
            out = oper.execute("show arp")
        assert "Total entries: 0" in out

    def test_single_reachable_entry(self, oper):
        links = [_MockLink("ens33", 2, 0, 1500, "UP")]
        nbrs = [_MockNeighbour("172.18.4.41", "00:50:56:95:0b:2b", 2, _NUD_REACHABLE_T)]
        mock_ip = _make_arp_iproute_mock(links, nbrs)
        with patch(_PATCH_IPROUTE, mock_ip):
            out = oper.execute("show arp")
        assert "00:50:56:95:0b:2b" in out
        assert "172.18.4.41" in out
        assert "ens33" in out
        assert "none" in out
        assert "Total entries: 1" in out

    def test_multiple_entries(self, oper):
        links = [
            _MockLink("ens33", 2, 0, 1500, "UP"),
            _MockLink("ens34", 3, 0, 1500, "UP"),
        ]
        nbrs = [
            _MockNeighbour("172.18.4.41", "00:50:56:95:0b:2b", 2, _NUD_REACHABLE_T),
            _MockNeighbour("10.0.0.2", "00:50:56:95:ba:0f", 3, _NUD_STALE_T),
        ]
        mock_ip = _make_arp_iproute_mock(links, nbrs)
        with patch(_PATCH_IPROUTE, mock_ip):
            out = oper.execute("show arp")
        assert "00:50:56:95:0b:2b" in out
        assert "00:50:56:95:ba:0f" in out
        assert "ens33" in out
        assert "ens34" in out
        assert "Total entries: 2" in out

    def test_entries_sorted_by_ip(self, oper):
        links = [
            _MockLink("ens33", 2, 0, 1500, "UP"),
            _MockLink("ens34", 3, 0, 1500, "UP"),
        ]
        nbrs = [
            _MockNeighbour("172.18.4.41", "00:50:56:95:0b:2b", 2, _NUD_REACHABLE_T),
            _MockNeighbour("10.0.0.2", "00:50:56:95:ba:0f", 3, _NUD_REACHABLE_T),
        ]
        mock_ip = _make_arp_iproute_mock(links, nbrs)
        with patch(_PATCH_IPROUTE, mock_ip):
            out = oper.execute("show arp")
        assert out.index("10.0.0.2") < out.index("172.18.4.41")

    def test_exact_column_format(self, oper):
        links = [_MockLink("ens33", 2, 0, 1500, "UP")]
        nbrs = [_MockNeighbour("172.18.4.41", "00:50:56:95:0b:2b", 2, _NUD_REACHABLE_T)]
        mock_ip = _make_arp_iproute_mock(links, nbrs)
        with patch(_PATCH_IPROUTE, mock_ip):
            out = oper.execute("show arp")
        data_line = [ln for ln in out.splitlines() if "00:50:56" in ln][0]
        assert data_line == (
            f"{'00:50:56:95:0b:2b':<18}{'172.18.4.41':<16}"
            f"{'172.18.4.41':<16}{'ens33':<13}none"
        )

    # ------------------------------------------------------------------
    # NUD state filtering
    # ------------------------------------------------------------------

    def test_stale_entry_shown(self, oper):
        links = [_MockLink("ens33", 2, 0, 1500, "UP")]
        nbrs = [_MockNeighbour("10.0.0.1", "aa:bb:cc:dd:ee:ff", 2, _NUD_STALE_T)]
        mock_ip = _make_arp_iproute_mock(links, nbrs)
        with patch(_PATCH_IPROUTE, mock_ip):
            out = oper.execute("show arp")
        assert "aa:bb:cc:dd:ee:ff" in out

    def test_delay_entry_shown(self, oper):
        links = [_MockLink("ens33", 2, 0, 1500, "UP")]
        nbrs = [_MockNeighbour("10.0.0.1", "aa:bb:cc:dd:ee:ff", 2, _NUD_DELAY_T)]
        mock_ip = _make_arp_iproute_mock(links, nbrs)
        with patch(_PATCH_IPROUTE, mock_ip):
            out = oper.execute("show arp")
        assert "aa:bb:cc:dd:ee:ff" in out

    def test_probe_entry_shown(self, oper):
        links = [_MockLink("ens33", 2, 0, 1500, "UP")]
        nbrs = [_MockNeighbour("10.0.0.1", "aa:bb:cc:dd:ee:ff", 2, _NUD_PROBE_T)]
        mock_ip = _make_arp_iproute_mock(links, nbrs)
        with patch(_PATCH_IPROUTE, mock_ip):
            out = oper.execute("show arp")
        assert "aa:bb:cc:dd:ee:ff" in out

    def test_permanent_entry_shown(self, oper):
        links = [_MockLink("ens33", 2, 0, 1500, "UP")]
        nbrs = [_MockNeighbour("10.0.0.1", "aa:bb:cc:dd:ee:ff", 2, _NUD_PERMANENT_T)]
        mock_ip = _make_arp_iproute_mock(links, nbrs)
        with patch(_PATCH_IPROUTE, mock_ip):
            out = oper.execute("show arp")
        assert "aa:bb:cc:dd:ee:ff" in out

    def test_incomplete_entry_skipped(self, oper):
        links = [_MockLink("ens33", 2, 0, 1500, "UP")]
        nbrs = [_MockNeighbour("10.0.0.1", "aa:bb:cc:dd:ee:ff", 2, _NUD_INCOMPLETE)]
        mock_ip = _make_arp_iproute_mock(links, nbrs)
        with patch(_PATCH_IPROUTE, mock_ip):
            out = oper.execute("show arp")
        assert "aa:bb:cc:dd:ee:ff" not in out
        assert "Total entries: 0" in out

    def test_failed_entry_skipped(self, oper):
        links = [_MockLink("ens33", 2, 0, 1500, "UP")]
        nbrs = [_MockNeighbour("10.0.0.1", "aa:bb:cc:dd:ee:ff", 2, _NUD_FAILED)]
        mock_ip = _make_arp_iproute_mock(links, nbrs)
        with patch(_PATCH_IPROUTE, mock_ip):
            out = oper.execute("show arp")
        assert "aa:bb:cc:dd:ee:ff" not in out

    def test_noarp_entry_skipped(self, oper):
        links = [_MockLink("ens33", 2, 0, 1500, "UP")]
        nbrs = [_MockNeighbour("10.0.0.1", "aa:bb:cc:dd:ee:ff", 2, _NUD_NOARP)]
        mock_ip = _make_arp_iproute_mock(links, nbrs)
        with patch(_PATCH_IPROUTE, mock_ip):
            out = oper.execute("show arp")
        assert "aa:bb:cc:dd:ee:ff" not in out

    def test_entry_without_mac_skipped(self, oper):
        """Entries with no NDA_LLADDR (e.g. incomplete) are skipped."""
        links = [_MockLink("ens33", 2, 0, 1500, "UP")]
        nbr = _MockNeighbour("10.0.0.1", "aa:bb:cc:dd:ee:ff", 2, _NUD_REACHABLE_T)
        nbr._a["NDA_LLADDR"] = None  # strip the MAC
        mock_ip = _make_arp_iproute_mock(links, [nbr])
        with patch(_PATCH_IPROUTE, mock_ip):
            out = oper.execute("show arp")
        assert "Total entries: 0" in out

    # ------------------------------------------------------------------
    # Interface name resolution
    # ------------------------------------------------------------------

    def test_ifindex_resolved_to_name(self, oper):
        links = [_MockLink("ens34", 3, 0, 1500, "UP")]
        nbrs = [_MockNeighbour("10.0.0.2", "00:50:56:95:ba:0f", 3, _NUD_REACHABLE_T)]
        mock_ip = _make_arp_iproute_mock(links, nbrs)
        with patch(_PATCH_IPROUTE, mock_ip):
            out = oper.execute("show arp")
        assert "ens34" in out

    def test_unknown_ifindex_shown_as_ifN(self, oper):
        """When no link matches the ifindex, fall back to 'if<N>' notation."""
        nbrs = [_MockNeighbour("10.0.0.2", "00:50:56:95:ba:0f", 99, _NUD_REACHABLE_T)]
        mock_ip = _make_arp_iproute_mock([], nbrs)
        with patch(_PATCH_IPROUTE, mock_ip):
            out = oper.execute("show arp")
        assert "if99" in out

    # ------------------------------------------------------------------
    # Filter by interface
    # ------------------------------------------------------------------

    def test_filter_interface_keeps_matching(self, oper):
        links = [
            _MockLink("ens33", 2, 0, 1500, "UP"),
            _MockLink("ens34", 3, 0, 1500, "UP"),
        ]
        nbrs = [
            _MockNeighbour("172.18.4.41", "00:50:56:95:0b:2b", 2, _NUD_REACHABLE_T),
            _MockNeighbour("10.0.0.2", "00:50:56:95:ba:0f", 3, _NUD_REACHABLE_T),
        ]
        mock_ip = _make_arp_iproute_mock(links, nbrs)
        with patch(_PATCH_IPROUTE, mock_ip):
            out = oper.execute("show arp interface ens33")
        assert "00:50:56:95:0b:2b" in out
        assert "00:50:56:95:ba:0f" not in out
        assert "Total entries: 1" in out

    def test_filter_interface_no_match(self, oper):
        links = [_MockLink("ens33", 2, 0, 1500, "UP")]
        nbrs = [_MockNeighbour("172.18.4.41", "00:50:56:95:0b:2b", 2, _NUD_REACHABLE_T)]
        mock_ip = _make_arp_iproute_mock(links, nbrs)
        with patch(_PATCH_IPROUTE, mock_ip):
            out = oper.execute("show arp interface ens99")
        assert "Total entries: 0" in out

    def test_filter_interface_missing_arg_returns_error(self, oper):
        mock_ip = _make_arp_iproute_mock([], [])
        with patch(_PATCH_IPROUTE, mock_ip):
            out = oper.execute("show arp interface")
        assert "error" in out.lower()

    # ------------------------------------------------------------------
    # Filter by hostname (IP)
    # ------------------------------------------------------------------

    def test_filter_hostname_keeps_matching(self, oper):
        links = [
            _MockLink("ens33", 2, 0, 1500, "UP"),
            _MockLink("ens34", 3, 0, 1500, "UP"),
        ]
        nbrs = [
            _MockNeighbour("172.18.4.41", "00:50:56:95:0b:2b", 2, _NUD_REACHABLE_T),
            _MockNeighbour("10.0.0.2", "00:50:56:95:ba:0f", 3, _NUD_REACHABLE_T),
        ]
        mock_ip = _make_arp_iproute_mock(links, nbrs)
        with patch(_PATCH_IPROUTE, mock_ip):
            out = oper.execute("show arp hostname 172.18.4.41")
        assert "00:50:56:95:0b:2b" in out
        assert "00:50:56:95:ba:0f" not in out
        assert "Total entries: 1" in out

    def test_filter_hostname_no_match(self, oper):
        links = [_MockLink("ens33", 2, 0, 1500, "UP")]
        nbrs = [_MockNeighbour("172.18.4.41", "00:50:56:95:0b:2b", 2, _NUD_REACHABLE_T)]
        mock_ip = _make_arp_iproute_mock(links, nbrs)
        with patch(_PATCH_IPROUTE, mock_ip):
            out = oper.execute("show arp hostname 1.2.3.4")
        assert "Total entries: 0" in out

    def test_filter_hostname_missing_arg_returns_error(self, oper):
        mock_ip = _make_arp_iproute_mock([], [])
        with patch(_PATCH_IPROUTE, mock_ip):
            out = oper.execute("show arp hostname")
        assert "error" in out.lower()

    # ------------------------------------------------------------------
    # Error paths
    # ------------------------------------------------------------------

    def test_unknown_option_returns_error(self, oper):
        mock_ip = _make_arp_iproute_mock([], [])
        with patch(_PATCH_IPROUTE, mock_ip):
            out = oper.execute("show arp bogus")
        assert "error" in out.lower()

    def test_pyroute2_unavailable_returns_error(self, oper):
        with patch(_PATCH_IPROUTE, None):
            out = oper.execute("show arp")
        assert "error" in out.lower()

    def test_kernel_error_returns_error(self, oper):
        broken = Mock()
        broken.__enter__ = Mock(return_value=broken)
        broken.__exit__ = Mock(return_value=False)
        broken.get_links.side_effect = OSError("eperm")
        with patch(_PATCH_IPROUTE, Mock(return_value=broken)):
            out = oper.execute("show arp")
        assert "error" in out.lower()


# ============================================================================
# show arp — tab completion
# ============================================================================

class TestShowArpCompletion:
    def test_show_space_offers_arp(self):
        kws = complete_oper("show ")
        assert "arp" in kws

    def test_show_a_partial_completes_arp(self):
        kws = complete_oper("show a")
        assert "arp" in kws

    def test_show_arp_space_offers_interface(self):
        kws = complete_oper("show arp ")
        assert "interface" in kws

    def test_show_arp_space_offers_hostname(self):
        kws = complete_oper("show arp ")
        assert "hostname" in kws

    def test_show_arp_partial_i_offers_interface(self):
        kws = complete_oper("show arp i")
        assert "interface" in kws
        assert "hostname" not in kws

    def test_show_arp_partial_h_offers_hostname(self):
        kws = complete_oper("show arp h")
        assert "hostname" in kws
        assert "interface" not in kws

    def test_show_arp_interface_space_offers_hint(self):
        kws = complete_oper("show arp interface ")
        assert any("<interface-name>" in k for k in kws)

    def test_show_arp_hostname_space_offers_hint(self):
        kws = complete_oper("show arp hostname ")
        assert any("<ip-address>" in k for k in kws)
