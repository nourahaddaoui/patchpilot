"""Day 6: retries, rollback after red tests, cleanup after partial failure."""

import json
import subprocess
from dataclasses import replace

from agent.loop import run
from agent.model import ScriptedModel
from agent.stop_rule import Limits, StopReason
from agent.tools import registry
from tests.test_loop import FIX, logger, make_ctx, tool_records, work, world  # noqa: F401  (fixtures)


def events(logger, action):
    return [r for r in logger.read() if r.get("event") == "recovery" and r.get("action") == action]


def req(work):
    return (work / "requirements.txt").read_text()


def test_red_tests_roll_the_edit_back_and_the_model_is_told(world, make_ctx, logger, work):
    world.tests_green = False
    model = ScriptedModel(FIX[:4] + [("read_file", {"path": "requirements.txt"})] * 10)
    result = run(model, make_ctx(True), Limits(max_steps=5), logger)
    assert req(work).strip() == "requests==2.25.1"  # back to the starting file
    assert len(events(logger, "rollback")) == 1
    tests_reply = [m for m in result.messages if m["role"] == "tool"][3]["content"]
    assert "[recovery]" in tests_reply and "rolled back" in tests_reply
    assert result.state.failed_fixes == 1


def test_untested_edit_is_reverted_when_the_run_stops_early(world, make_ctx, logger, work):
    # step limit hits right after the edit, before any test run
    result = run(ScriptedModel(FIX), make_ctx(True), Limits(max_steps=3), logger)
    assert result.stop_reason is StopReason.STEP_LIMIT
    assert req(work).strip() == "requests==2.25.1"
    assert logger.read()[-1]["reverted_uncommitted_edit"] is True
    assert len(events(logger, "revert_partial")) == 1


def test_done_run_keeps_and_commits_its_fix(world, make_ctx, logger, work):
    result = run(ScriptedModel(FIX), make_ctx(True), logger=logger)
    assert result.stop_reason is StopReason.DONE and result.commit
    assert "2.31.0" in req(work)
    assert logger.read()[-1]["reverted_uncommitted_edit"] is False
    status = subprocess.run(["git", "status", "--porcelain"], cwd=work, capture_output=True, text=True).stdout
    assert status.strip() == ""


def test_tested_good_edit_survives_a_later_denial(world, make_ctx, logger, work):
    """Recovery never undoes an edit the tests already passed on."""
    from tests.test_loop import Approver
    ctx = make_ctx(True)
    answers = iter([True, False])
    ctx.approve = lambda p: next(answers)
    world.fixed_version = "2.31.0"
    script = [("run_scan", {}), ("edit_file", {"path": "requirements.txt", "package": "requests", "version": "2.31.0"}),
              ("run_tests", {}), ("edit_file", {"path": "requirements.txt", "package": "requests", "version": "2.32.4"})]
    result = run(ScriptedModel(script), ctx, logger=logger)
    assert result.stop_reason is StopReason.APPROVAL_DENIED
    assert req(work).strip() == "requests==2.31.0"


def test_transient_scan_failure_is_retried_once(world, make_ctx, logger, monkeypatch):
    calls = {"n": 0}
    real = registry.TOOLS["run_scan"].run

    def flaky(args, ctx):
        calls["n"] += 1
        if calls["n"] == 1:
            from agent.tools.errors import ToolError
            raise ToolError("timeout", "the scan timed out")
        return real(args, ctx)

    monkeypatch.setitem(registry.TOOLS, "run_scan", replace(registry.TOOLS["run_scan"], run=flaky))
    result = run(ScriptedModel(FIX), make_ctx(True), logger=logger)
    assert result.stop_reason is StopReason.DONE
    assert len(events(logger, "retry")) == 1
    scans = [r for r in tool_records(logger) if r["tool"] == "run_scan"]
    assert [r["ok"] for r in scans[:2]] == [False, True] and scans[0]["step"] == scans[1]["step"] == 1


def test_permanent_failure_is_not_retried(world, make_ctx, logger, monkeypatch):
    from agent.tools.errors import ToolError

    def down(args, ctx):
        raise ToolError("tool_down", "trivy is not installed")

    monkeypatch.setitem(registry.TOOLS, "run_scan", replace(registry.TOOLS["run_scan"], run=down))
    run(ScriptedModel(FIX[:1] * 3), make_ctx(True), Limits(max_steps=1), logger)
    assert events(logger, "retry") == []


def test_write_tools_are_never_retried(world, make_ctx, logger, monkeypatch):
    from agent.tools.errors import ToolError
    calls = {"n": 0}

    def boom(args, ctx):
        calls["n"] += 1
        raise ToolError("timeout", "slow disk")

    monkeypatch.setitem(registry.TOOLS, "edit_file", replace(registry.TOOLS["edit_file"], run=boom))
    run(ScriptedModel([FIX[2]]), make_ctx(True), Limits(max_steps=1), logger)
    assert calls["n"] == 1
