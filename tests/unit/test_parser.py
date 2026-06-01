"""Unit tests for nos.cli.parser."""
import pytest

from nos.cli.parser import CLIMode, CommandParser, CommandType


@pytest.fixture
def parser():
    return CommandParser()


# ============================================================================
# Operational mode — basic commands
# ============================================================================

class TestOperationalMode:
    def test_show_no_args(self, parser):
        r = parser.parse("show", CLIMode.OPERATIONAL)
        assert r.command == CommandType.SHOW
        assert r.args == []
        assert r.pipe is None

    def test_show_interfaces(self, parser):
        r = parser.parse("show interfaces", CLIMode.OPERATIONAL)
        assert r.command == CommandType.SHOW
        assert r.args == ["interfaces"]

    def test_show_with_pipe_match(self, parser):
        r = parser.parse("show interfaces | match ge-", CLIMode.OPERATIONAL)
        assert r.command == CommandType.SHOW
        assert r.args == ["interfaces"]
        assert r.pipe == "match ge-"

    def test_show_with_pipe_except(self, parser):
        r = parser.parse("show route | except 0.0.0.0", CLIMode.OPERATIONAL)
        assert r.command == CommandType.SHOW
        assert r.pipe == "except 0.0.0.0"

    def test_show_with_pipe_count(self, parser):
        r = parser.parse("show bgp summary | count", CLIMode.OPERATIONAL)
        assert r.command == CommandType.SHOW
        assert r.pipe == "count"

    def test_show_with_pipe_no_more(self, parser):
        r = parser.parse("show route | no-more", CLIMode.OPERATIONAL)
        assert r.command == CommandType.SHOW
        assert r.pipe == "no-more"

    def test_ping(self, parser):
        r = parser.parse("ping 10.0.0.1", CLIMode.OPERATIONAL)
        assert r.command == CommandType.PING
        assert r.args == ["10.0.0.1"]

    def test_ping_no_args(self, parser):
        r = parser.parse("ping", CLIMode.OPERATIONAL)
        assert r.command == CommandType.PING
        assert r.args == []

    def test_traceroute(self, parser):
        r = parser.parse("traceroute 192.168.1.1", CLIMode.OPERATIONAL)
        assert r.command == CommandType.TRACEROUTE
        assert r.args == ["192.168.1.1"]

    def test_tracert_alias(self, parser):
        r = parser.parse("tracert 192.168.1.1", CLIMode.OPERATIONAL)
        assert r.command == CommandType.TRACEROUTE

    def test_configure(self, parser):
        r = parser.parse("configure", CLIMode.OPERATIONAL)
        assert r.command == CommandType.CONFIGURE
        assert not r.is_error

    def test_exit(self, parser):
        r = parser.parse("exit", CLIMode.OPERATIONAL)
        assert r.command == CommandType.EXIT

    def test_quit(self, parser):
        r = parser.parse("quit", CLIMode.OPERATIONAL)
        assert r.command == CommandType.EXIT

    def test_unknown_command(self, parser):
        r = parser.parse("foobar", CLIMode.OPERATIONAL)
        assert r.command == CommandType.UNKNOWN
        assert r.is_error

    def test_empty_line(self, parser):
        r = parser.parse("", CLIMode.OPERATIONAL)
        assert r.command == CommandType.UNKNOWN
        assert not r.is_error

    def test_whitespace_only(self, parser):
        r = parser.parse("   ", CLIMode.OPERATIONAL)
        assert r.command == CommandType.UNKNOWN

    def test_case_insensitive_command(self, parser):
        r = parser.parse("SHOW interfaces", CLIMode.OPERATIONAL)
        assert r.command == CommandType.SHOW
        assert r.args == ["interfaces"]

    def test_show_bgp_summary(self, parser):
        r = parser.parse("show bgp summary", CLIMode.OPERATIONAL)
        assert r.command == CommandType.SHOW
        assert r.args == ["bgp", "summary"]

    def test_show_isis_adjacency(self, parser):
        r = parser.parse("show isis adjacency", CLIMode.OPERATIONAL)
        assert r.command == CommandType.SHOW
        assert r.args == ["isis", "adjacency"]


# ============================================================================
# Configure mode — navigation commands
# ============================================================================

class TestConfigureModeNavigation:
    def test_edit(self, parser):
        r = parser.parse("edit interfaces eth0", CLIMode.CONFIGURE)
        assert r.command == CommandType.EDIT
        assert r.args == ["interfaces", "eth0"]

    def test_edit_no_args(self, parser):
        r = parser.parse("edit", CLIMode.CONFIGURE)
        assert r.command == CommandType.EDIT
        assert r.args == []

    def test_up_default(self, parser):
        r = parser.parse("up", CLIMode.CONFIGURE)
        assert r.command == CommandType.UP
        assert r.args == ["1"]

    def test_up_with_count(self, parser):
        r = parser.parse("up 3", CLIMode.CONFIGURE)
        assert r.command == CommandType.UP
        assert r.args == ["3"]

    def test_up_invalid(self, parser):
        r = parser.parse("up foo", CLIMode.CONFIGURE)
        assert r.command == CommandType.UNKNOWN
        assert r.is_error

    def test_top(self, parser):
        r = parser.parse("top", CLIMode.CONFIGURE)
        assert r.command == CommandType.TOP

    def test_discard(self, parser):
        r = parser.parse("discard", CLIMode.CONFIGURE)
        assert r.command == CommandType.DISCARD

    def test_exit_from_configure(self, parser):
        r = parser.parse("exit", CLIMode.CONFIGURE)
        assert r.command == CommandType.EXIT


# ============================================================================
# Configure mode — set / delete
# ============================================================================

class TestConfigureModeSetDelete:
    def test_set_simple(self, parser):
        r = parser.parse("set system host-name nos01", CLIMode.CONFIGURE)
        assert r.command == CommandType.SET
        assert r.args == ["system", "host-name", "nos01"]

    def test_set_quoted_value(self, parser):
        r = parser.parse('set interfaces eth0 description "my uplink"',
                          CLIMode.CONFIGURE)
        assert r.command == CommandType.SET
        assert r.args == ["interfaces", "eth0", "description", "my uplink"]

    def test_set_ip_address(self, parser):
        r = parser.parse(
            "set interfaces eth0 family inet address 10.0.0.1/30",
            CLIMode.CONFIGURE,
        )
        assert r.command == CommandType.SET
        assert "10.0.0.1/30" in r.args

    def test_set_no_args(self, parser):
        r = parser.parse("set", CLIMode.CONFIGURE)
        assert r.command == CommandType.SET
        assert r.args == []

    def test_delete(self, parser):
        r = parser.parse("delete interfaces eth0 description",
                          CLIMode.CONFIGURE)
        assert r.command == CommandType.DELETE
        assert r.args == ["interfaces", "eth0", "description"]

    def test_delete_no_args(self, parser):
        r = parser.parse("delete", CLIMode.CONFIGURE)
        assert r.command == CommandType.DELETE
        assert r.args == []


# ============================================================================
# Configure mode — show
# ============================================================================

class TestConfigureModeShow:
    def test_show_no_args(self, parser):
        r = parser.parse("show", CLIMode.CONFIGURE)
        assert r.command == CommandType.SHOW
        assert r.args == []
        assert r.pipe is None

    def test_show_with_compare_pipe(self, parser):
        r = parser.parse("show | compare", CLIMode.CONFIGURE)
        assert r.command == CommandType.SHOW
        assert r.pipe == "compare"

    def test_show_interfaces(self, parser):
        r = parser.parse("show interfaces", CLIMode.CONFIGURE)
        assert r.command == CommandType.SHOW
        assert r.args == ["interfaces"]


# ============================================================================
# Configure mode — commit variants
# ============================================================================

class TestConfigureModeCommit:
    def test_commit_plain(self, parser):
        r = parser.parse("commit", CLIMode.CONFIGURE)
        assert r.command == CommandType.COMMIT
        assert r.args == []

    def test_commit_check(self, parser):
        r = parser.parse("commit check", CLIMode.CONFIGURE)
        assert r.command == CommandType.COMMIT_CHECK

    def test_commit_confirmed(self, parser):
        r = parser.parse("commit confirmed 5", CLIMode.CONFIGURE)
        assert r.command == CommandType.COMMIT_CONFIRMED
        assert r.args == ["5"]

    def test_commit_confirmed_10(self, parser):
        r = parser.parse("commit confirmed 10", CLIMode.CONFIGURE)
        assert r.command == CommandType.COMMIT_CONFIRMED
        assert r.args == ["10"]

    def test_commit_confirmed_missing_minutes(self, parser):
        r = parser.parse("commit confirmed", CLIMode.CONFIGURE)
        assert r.command == CommandType.UNKNOWN
        assert r.is_error

    def test_commit_confirmed_zero_minutes(self, parser):
        r = parser.parse("commit confirmed 0", CLIMode.CONFIGURE)
        assert r.command == CommandType.UNKNOWN
        assert r.is_error

    def test_commit_confirmed_invalid(self, parser):
        r = parser.parse("commit confirmed abc", CLIMode.CONFIGURE)
        assert r.command == CommandType.UNKNOWN
        assert r.is_error

    def test_commit_unknown_subcommand(self, parser):
        r = parser.parse("commit save", CLIMode.CONFIGURE)
        assert r.command == CommandType.UNKNOWN
        assert r.is_error


# ============================================================================
# Configure mode — rollback
# ============================================================================

class TestConfigureModeRollback:
    def test_rollback_zero(self, parser):
        r = parser.parse("rollback 0", CLIMode.CONFIGURE)
        assert r.command == CommandType.ROLLBACK
        assert r.args == ["0"]

    def test_rollback_49(self, parser):
        r = parser.parse("rollback 49", CLIMode.CONFIGURE)
        assert r.command == CommandType.ROLLBACK
        assert r.args == ["49"]

    def test_rollback_out_of_range(self, parser):
        r = parser.parse("rollback 50", CLIMode.CONFIGURE)
        assert r.command == CommandType.UNKNOWN
        assert r.is_error

    def test_rollback_negative(self, parser):
        r = parser.parse("rollback -1", CLIMode.CONFIGURE)
        assert r.command == CommandType.UNKNOWN
        assert r.is_error

    def test_rollback_no_args(self, parser):
        r = parser.parse("rollback", CLIMode.CONFIGURE)
        assert r.command == CommandType.UNKNOWN
        assert r.is_error

    def test_rollback_string(self, parser):
        r = parser.parse("rollback last", CLIMode.CONFIGURE)
        assert r.command == CommandType.UNKNOWN
        assert r.is_error


# ============================================================================
# Configure mode — run
# ============================================================================

class TestConfigureModeRun:
    def test_run_show(self, parser):
        r = parser.parse("run show interfaces", CLIMode.CONFIGURE)
        assert r.command == CommandType.RUN
        assert r.args == ["show", "interfaces"]

    def test_run_ping(self, parser):
        r = parser.parse("run ping 10.0.0.1", CLIMode.CONFIGURE)
        assert r.command == CommandType.RUN
        assert r.args == ["ping", "10.0.0.1"]

    def test_run_no_args(self, parser):
        r = parser.parse("run", CLIMode.CONFIGURE)
        assert r.command == CommandType.RUN
        assert r.args == []


# ============================================================================
# Quoted string handling
# ============================================================================

class TestQuotedStrings:
    def test_quoted_description(self, parser):
        r = parser.parse(
            'set system host-name "my router"', CLIMode.CONFIGURE
        )
        assert r.command == CommandType.SET
        assert "my router" in r.args

    def test_quoted_with_special_chars(self, parser):
        r = parser.parse(
            'set interfaces eth0 description "uplink to router-01"',
            CLIMode.CONFIGURE,
        )
        assert r.command == CommandType.SET
        assert "uplink to router-01" in r.args

    def test_unclosed_quote_returns_error(self, parser):
        r = parser.parse('set interfaces eth0 description "unclosed',
                          CLIMode.CONFIGURE)
        assert r.command == CommandType.UNKNOWN
        assert r.is_error

    def test_raw_field(self, parser):
        r = parser.parse("show interfaces", CLIMode.OPERATIONAL)
        assert r.raw == "show interfaces"
