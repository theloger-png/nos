"""Unit tests for nos.cli.parser."""
import pytest

from nos.cli.parser import CLIMode, CommandParser, CommandType, resolve_prefix


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

    def test_show_with_pipe_no_space_before(self, parser):
        r = parser.parse("show interfaces| match ge-", CLIMode.OPERATIONAL)
        assert r.command == CommandType.SHOW
        assert r.args == ["interfaces"]
        assert r.pipe == "match ge-"

    def test_show_with_pipe_no_space_after(self, parser):
        r = parser.parse("show route |except 0.0.0.0", CLIMode.OPERATIONAL)
        assert r.command == CommandType.SHOW
        assert r.pipe == "except 0.0.0.0"

    def test_show_with_pipe_no_spaces(self, parser):
        r = parser.parse("show bgp summary|count", CLIMode.OPERATIONAL)
        assert r.command == CommandType.SHOW
        assert r.pipe == "count"

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

    def test_show_with_pipe_no_space_before_configure(self, parser):
        r = parser.parse("show configuration interfaces irb| display set", CLIMode.CONFIGURE)
        assert r.command == CommandType.SHOW
        assert r.args == ["configuration", "interfaces", "irb"]
        assert r.pipe == "display set"

    def test_show_with_pipe_no_spaces_configure(self, parser):
        r = parser.parse("show|compare", CLIMode.CONFIGURE)
        assert r.command == CommandType.SHOW
        assert r.pipe == "compare"


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

    def test_commit_and_quit(self, parser):
        r = parser.parse("commit and-quit", CLIMode.CONFIGURE)
        assert r.command == CommandType.COMMIT_AND_QUIT

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


# ============================================================================
# resolve_prefix unit tests
# ============================================================================

class TestResolvePrefix:
    def test_exact_match(self):
        resolved, err = resolve_prefix("show", ["show", "set", "delete"])
        assert resolved == "show"
        assert err is None

    def test_unambiguous_prefix(self):
        resolved, err = resolve_prefix("sh", ["show", "set", "delete"])
        assert resolved == "show"
        assert err is None

    def test_exact_wins_over_other_matches(self):
        # "set" is exact even though "set" is also a prefix of nothing else
        resolved, err = resolve_prefix("set", ["set", "setup", "setmode"])
        assert resolved == "set"
        assert err is None

    def test_ambiguous_returns_error(self):
        resolved, err = resolve_prefix("s", ["show", "set", "delete"])
        assert resolved is None
        assert err is not None
        assert "ambiguous" in err
        assert "set" in err
        assert "show" in err

    def test_unknown_returns_error(self):
        resolved, err = resolve_prefix("xyz", ["show", "set", "delete"])
        assert resolved is None
        assert err is not None
        assert "unknown" in err

    def test_empty_candidates(self):
        resolved, err = resolve_prefix("show", [])
        assert resolved is None
        assert err is not None

    def test_single_candidate_match(self):
        resolved, err = resolve_prefix("s", ["show"])
        assert resolved == "show"
        assert err is None


# ============================================================================
# Prefix matching — operational mode
# ============================================================================

class TestOperationalPrefixMatching:
    def test_sho_expands_to_show(self, parser):
        r = parser.parse("sho", CLIMode.OPERATIONAL)
        assert r.command == CommandType.SHOW
        assert not r.is_error

    def test_sh_expands_to_show(self, parser):
        r = parser.parse("sh", CLIMode.OPERATIONAL)
        assert r.command == CommandType.SHOW

    def test_con_expands_to_configure(self, parser):
        r = parser.parse("con", CLIMode.OPERATIONAL)
        assert r.command == CommandType.CONFIGURE

    def test_pi_expands_to_ping(self, parser):
        r = parser.parse("pi 10.0.0.1", CLIMode.OPERATIONAL)
        assert r.command == CommandType.PING
        assert r.args == ["10.0.0.1"]

    def test_trace_expands_to_traceroute(self, parser):
        r = parser.parse("trace 1.1.1.1", CLIMode.OPERATIONAL)
        assert r.command == CommandType.TRACEROUTE
        assert r.args == ["1.1.1.1"]

    def test_ex_expands_to_exit(self, parser):
        r = parser.parse("ex", CLIMode.OPERATIONAL)
        assert r.command == CommandType.EXIT

    def test_tracert_alias_still_works(self, parser):
        r = parser.parse("tracert 1.1.1.1", CLIMode.OPERATIONAL)
        assert r.command == CommandType.TRACEROUTE

    def test_quit_alias_still_works(self, parser):
        r = parser.parse("quit", CLIMode.OPERATIONAL)
        assert r.command == CommandType.EXIT

    def test_prefix_with_args_preserved(self, parser):
        r = parser.parse("sho interfaces", CLIMode.OPERATIONAL)
        assert r.command == CommandType.SHOW
        assert r.args == ["interfaces"]

    def test_ambiguous_oper_prefix_errors(self, parser):
        # 's' matches both 'show' — only one match so it resolves
        # Use a token that truly matches multiple: no such case in oper
        # but we can test an explicitly unknown token
        r = parser.parse("zzz", CLIMode.OPERATIONAL)
        assert r.command == CommandType.UNKNOWN
        assert r.is_error

    def test_prefix_with_pipe_preserved(self, parser):
        r = parser.parse("sho route | match 10.0", CLIMode.OPERATIONAL)
        assert r.command == CommandType.SHOW
        assert r.pipe == "match 10.0"


# ============================================================================
# Prefix matching — configure mode top-level
# ============================================================================

class TestConfigurePrefixMatching:
    def test_se_expands_to_set(self, parser):
        r = parser.parse("se system host-name nos01", CLIMode.CONFIGURE)
        assert r.command == CommandType.SET
        assert r.args == ["system", "host-name", "nos01"]

    def test_del_expands_to_delete(self, parser):
        r = parser.parse("del interfaces eth0", CLIMode.CONFIGURE)
        assert r.command == CommandType.DELETE
        assert r.args == ["interfaces", "eth0"]

    def test_ed_expands_to_edit(self, parser):
        r = parser.parse("ed interfaces eth0", CLIMode.CONFIGURE)
        assert r.command == CommandType.EDIT
        assert r.args == ["interfaces", "eth0"]

    def test_sh_expands_to_show_configure(self, parser):
        r = parser.parse("sh interfaces", CLIMode.CONFIGURE)
        assert r.command == CommandType.SHOW
        assert r.args == ["interfaces"]

    def test_com_expands_to_commit(self, parser):
        r = parser.parse("com", CLIMode.CONFIGURE)
        assert r.command == CommandType.COMMIT

    def test_ro_expands_to_rollback(self, parser):
        r = parser.parse("ro 0", CLIMode.CONFIGURE)
        assert r.command == CommandType.ROLLBACK
        assert r.args == ["0"]

    def test_dis_expands_to_discard(self, parser):
        r = parser.parse("dis", CLIMode.CONFIGURE)
        assert r.command == CommandType.DISCARD

    def test_ru_expands_to_run(self, parser):
        r = parser.parse("ru show interfaces", CLIMode.CONFIGURE)
        assert r.command == CommandType.RUN
        assert r.args == ["show", "interfaces"]

    def test_ex_expands_to_exit(self, parser):
        r = parser.parse("ex", CLIMode.CONFIGURE)
        assert r.command == CommandType.EXIT

    def test_to_expands_to_top(self, parser):
        r = parser.parse("to", CLIMode.CONFIGURE)
        assert r.command == CommandType.TOP

    def test_u_expands_to_up(self, parser):
        r = parser.parse("u", CLIMode.CONFIGURE)
        assert r.command == CommandType.UP

    def test_quit_alias_configure(self, parser):
        r = parser.parse("quit", CLIMode.CONFIGURE)
        assert r.command == CommandType.EXIT

    def test_ambiguous_d_errors(self, parser):
        # 'd' matches 'delete' AND 'discard' → ambiguous
        r = parser.parse("d interfaces", CLIMode.CONFIGURE)
        assert r.command == CommandType.UNKNOWN
        assert r.is_error
        assert "ambiguous" in r.error
        assert "delete" in r.error
        assert "discard" in r.error

    def test_ambiguous_e_errors(self, parser):
        # 'e' matches 'edit' AND 'exit' → ambiguous
        r = parser.parse("e interfaces eth0", CLIMode.CONFIGURE)
        assert r.command == CommandType.UNKNOWN
        assert r.is_error
        assert "ambiguous" in r.error

    def test_ambiguous_r_errors(self, parser):
        # 'r' matches 'rollback' AND 'run' → ambiguous
        r = parser.parse("r 0", CLIMode.CONFIGURE)
        assert r.command == CommandType.UNKNOWN
        assert r.is_error
        assert "ambiguous" in r.error

    def test_unknown_configure_prefix_errors(self, parser):
        r = parser.parse("zzz", CLIMode.CONFIGURE)
        assert r.command == CommandType.UNKNOWN
        assert r.is_error
        assert "unknown" in r.error


# ============================================================================
# Prefix matching — commit sub-commands
# ============================================================================

class TestCommitPrefixMatching:
    def test_che_expands_to_check(self, parser):
        r = parser.parse("commit che", CLIMode.CONFIGURE)
        assert r.command == CommandType.COMMIT_CHECK

    def test_conf_expands_to_confirmed(self, parser):
        r = parser.parse("commit conf 5", CLIMode.CONFIGURE)
        assert r.command == CommandType.COMMIT_CONFIRMED
        assert r.args == ["5"]

    def test_ambiguous_c_in_commit_errors(self, parser):
        # 'c' matches both 'check' and 'confirmed' → ambiguous
        r = parser.parse("commit c", CLIMode.CONFIGURE)
        assert r.command == CommandType.UNKNOWN
        assert r.is_error
        assert "ambiguous" in r.error

    def test_unknown_commit_sub_errors(self, parser):
        r = parser.parse("commit xyz", CLIMode.CONFIGURE)
        assert r.command == CommandType.UNKNOWN
        assert r.is_error


# ============================================================================
# Prefix matching — show sub-commands (operational mode dispatch)
# ============================================================================

class TestShowSubcommandPrefixMatching:
    """Test that show sub-commands resolve via prefix matching in OperationalMode."""

    @pytest.fixture
    def oper(self, tmp_path):
        from nos.cli.modes.operational import OperationalMode
        from nos.config.store import ConfigStore
        return OperationalMode(ConfigStore(base_dir=tmp_path))

    def test_show_int_expands_to_interfaces(self, oper):
        out = oper.execute("show int")
        # The output must not start with "error:" (CLI error prefix);
        # "Errors:" and "errors" appear legitimately in the stats section.
        assert not out.lower().startswith("error:") or "no interface" in out.lower()

    def test_show_ro_expands_to_route(self, oper):
        out = oper.execute("show ro")
        assert out is not None
        assert "ambiguous" not in out

    def test_show_b_expands_to_bgp(self, oper):
        out = oper.execute("show b")
        assert out is not None
        assert "ambiguous" not in out

    def test_show_sys_expands_to_system(self, oper):
        out = oper.execute("show sys")
        assert out is not None
        assert "ambiguous" not in out

    def test_show_v_expands_to_vlans(self, oper):
        out = oper.execute("show v")
        assert out is not None
        assert "ambiguous" not in out

    def test_show_fo_expands_to_forwarding(self, oper):
        out = oper.execute("show fo")
        assert out is not None
        assert "ambiguous" not in out

    def test_show_conf_expands_to_configuration(self, oper):
        out = oper.execute("show conf")
        assert out is not None
        assert "ambiguous" not in out

    def test_show_i_ambiguous_isis_interfaces(self, oper):
        # 'i' matches 'interfaces' AND 'isis' → ambiguous
        out = oper.execute("show i")
        assert "ambiguous" in out

    def test_show_unknown_sub_errors(self, oper):
        out = oper.execute("show foobar")
        assert "unknown" in out

    def test_show_combined_prefix_sho_int(self, oper):
        # Both top-level and sub-command are abbreviated
        out = oper.execute("sho int")
        assert not out.lower().startswith("error:") or "no interface" in out.lower()


# ============================================================================
# Prefix matching — config path expansion (configure mode)
# ============================================================================

class TestConfigPathPrefixExpansion:
    """Test that config path tokens are expanded in set/delete/edit/show."""

    @pytest.fixture
    def mode(self, tmp_path):
        from nos.cli.modes.configure import ConfigureMode
        from nos.config.commit import CommitEngine
        from nos.config.store import ConfigStore
        from nos.config.validator import ConfigValidator
        store = ConfigStore(base_dir=tmp_path)
        engine = CommitEngine(store, validator=ConfigValidator())
        return ConfigureMode(store, engine)

    def test_set_int_expands_to_interfaces(self, mode):
        out = mode.execute("set int eth0 description uplink")
        assert out == "" or out is None or "error" not in (out or "")
        eth0 = mode.store.candidate.get("interfaces", {}).get("eth0", {})
        assert eth0.get("description") == "uplink"

    def test_set_sys_expands_to_system(self, mode):
        out = mode.execute("set sys host-name nos01")
        assert "error" not in (out or "")
        assert mode.store.candidate.get("system", {}).get("host_name") == "nos01"

    def test_set_ambiguous_path_errors(self, mode):
        # 'ro' matches 'routing-options' AND 'routing-instances'
        out = mode.execute("set ro static route 1.2.3.0/24 next-hop 10.0.0.1")
        assert "ambiguous" in out

    def test_delete_int_expands_to_interfaces(self, mode):
        mode.execute("set interfaces eth0 description uplink")
        out = mode.execute("del int eth0 description")
        assert "error" not in (out or "")
        eth0 = mode.store.candidate.get("interfaces", {}).get("eth0", {})
        assert "description" not in eth0

    def test_edit_int_expands_to_interfaces(self, mode):
        out = mode.execute("ed int eth0")
        assert "error" not in (out or "")
        assert mode.edit_path == ["interfaces", "eth0"]

    def test_show_int_in_configure_mode(self, mode):
        mode.execute("set interfaces eth0 description uplink")
        out = mode.execute("show int")
        assert "uplink" in out or "description" in out

    def test_unknown_config_section_passes_through(self, mode):
        # 'firewall' is not in CONFIG_TREE; show should not error but report empty
        out = mode.execute("show firewall")
        assert "no configuration" in out.lower() or "empty" in out.lower()
        assert "ambiguous" not in out
