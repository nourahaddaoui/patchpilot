from agent.stop_rule import Limits, RunState, StopReason, check


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
