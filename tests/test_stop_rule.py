from agent.stop_rule import Limits, RunState, StopReason, check
from tests.test_loop import logger, work, world  # noqa: F401  (fixtures)


def test_fresh_run_continues():
    assert check(RunState()) is None


def test_done_needs_scan_clean_and_tests_green():
    assert check(RunState(scan_clean=True, tests_green=True)) is StopReason.DONE


def test_clean_scan_alone_is_not_done():
    assert check(RunState(scan_clean=True, tests_green=False)) is None
    assert check(RunState(scan_clean=False, tests_green=True)) is None


def test_step_limit_at_12():
    assert check(RunState(steps=11)) is None
    assert check(RunState(steps=12)) is StopReason.STEP_LIMIT


def test_cost_cap_at_050():
    assert check(RunState(cost_usd=0.49)) is None
    assert check(RunState(cost_usd=0.50)) is StopReason.COST_CAP


def test_three_failed_fixes():
    assert check(RunState(failed_fixes=2)) is None
    assert check(RunState(failed_fixes=3)) is StopReason.FIX_ATTEMPTS


def test_approval_denied_beats_everything():
    state = RunState(
        approval_denied=True,
        no_fixed_version=True,
        scan_clean=True,
        tests_green=True,
        failed_fixes=3,
        cost_usd=1.0,
        steps=12,
    )
    assert check(state) is StopReason.APPROVAL_DENIED


def test_no_fixed_version():
    assert check(RunState(no_fixed_version=True)) is StopReason.NO_FIXED_VERSION


def test_custom_limits():
    limits = Limits(max_steps=3, max_cost_usd=0.10, max_failed_fixes=1)
    assert check(RunState(steps=3), limits) is StopReason.STEP_LIMIT
    assert check(RunState(cost_usd=0.10), limits) is StopReason.COST_CAP
    assert check(RunState(failed_fixes=1), limits) is StopReason.FIX_ATTEMPTS


def test_stub_loop_that_never_finishes_halts_at_max_steps():
    limits = Limits()
    state = RunState()
    calls = 0
    while check(state, limits) is None:
        state.record_step("run_scan", cost_usd=0.0)
        calls += 1
        assert calls <= limits.max_steps, "loop exceeded the step limit"
    assert check(state, limits) is StopReason.STEP_LIMIT
    assert calls == limits.max_steps
    assert len(state.history) == limits.max_steps


# --- GitHub mode: DONE needs the merged PR ------------------------------------------

def _gh_state(**kw):
    kw.setdefault("github_mode", True)
    kw.setdefault("found_vulns", ["CVE-2023-32681"])
    return RunState(**kw)


def test_github_mode_is_not_done_before_merge():
    assert check(_gh_state(scan_clean=True, tests_green=True)) is None
    assert check(_gh_state(scan_clean=True, tests_green=True, pr_number=12, pr_opened=True)) is None


def test_github_mode_is_done_after_merge():
    assert check(_gh_state(scan_clean=True, tests_green=True, merged=True)) is StopReason.DONE


def test_github_mode_merge_alone_is_not_done():
    assert check(_gh_state(merged=True)) is None


def test_github_mode_repo_clean_from_the_start_is_done():
    assert check(_gh_state(found_vulns=[], scan_clean=True, tests_green=True)) is StopReason.DONE


def test_local_mode_is_unchanged():
    assert check(RunState(scan_clean=True, tests_green=True, found_vulns=["CVE-1"])) is StopReason.DONE
    assert check(RunState(scan_clean=True, tests_green=True, merged=False)) is StopReason.DONE


def test_twelve_steps_still_cap_github_mode():
    assert check(_gh_state(steps=8)) is None  # the full GitHub flow fits
    assert check(_gh_state(steps=12)) is StopReason.STEP_LIMIT
    assert check(_gh_state(steps=12, scan_clean=True, tests_green=True)) is StopReason.STEP_LIMIT


def test_github_run_that_never_merges_ends_at_the_step_limit(world, work, logger, monkeypatch):
    """Clean + green but no PR merged: the loop keeps going, and the 12-step cap still ends it."""
    import sys
    from agent.loop import run
    from agent.model import ScriptedModel
    from agent.tools.context import ToolContext
    from tests.test_loop import FIX, Approver
    monkeypatch.setenv("GITHUB_TOKEN", "t")
    ctx = ToolContext(repo_root=work, test_python=sys.executable, install_deps=False, approve=Approver(True),
                      issue_mode="github", github_repo="o/r", http=lambda *a: (_ for _ in ()).throw(AssertionError))
    result = run(ScriptedModel(FIX), ctx, logger=logger)  # repeats the last call (run_scan) forever
    assert result.state.scan_clean and result.state.tests_green and not result.state.merged
    assert result.stop_reason is StopReason.STEP_LIMIT and result.state.steps == 12
    assert result.commit is None


def test_two_silent_turns_in_a_row_end_the_run_as_gave_up():
    from agent.stop_rule import Limits, RunState, StopReason, check
    s = RunState()
    s.record_step("(no tool call)")
    assert check(s, Limits()) is None  # first silent turn: nudge
    s.record_step("(no tool call)")
    assert check(s, Limits()) is StopReason.GAVE_UP


def test_a_tool_call_resets_the_silent_turn_count():
    from agent.stop_rule import Limits, RunState, check
    s = RunState()
    for name in ["(no tool call)", "read_file", "(no tool call)"]:
        s.record_step(name)
    assert s.idle_turns == 1 and check(s, Limits()) is None
