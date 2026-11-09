"""The ReAct loop, driven by a scripted model.

read_file and edit_file run for real on a real sandbox clone. run_scan and
run_tests are replaced by fakes (no Trivy, no pip) that look at the sandbox's
requirements.txt, so the loop sees the effect of its own edits.
"""

import json
import shutil
import subprocess
import sys
from dataclasses import replace

import pytest

from agent import sandbox
from agent.calllog import CallLogger
from agent.loop import NUDGE, run
from agent.model import ModelReply, ScriptedModel, ToolCall
from agent.stop_rule import Limits, StopReason
from agent.tools import registry
from agent.tools.context import ToolContext
from tests.conftest import TEMPLATE

VULN = {"id": "CVE-2023-32681", "package": "requests", "installed": "2.25.1", "fixed": "2.31.0",
        "severity": "MEDIUM", "title": "Proxy-Authorization header leak", "file": "requirements.txt"}

FIX = [
    ("run_scan", {}),
    ("read_file", {"path": "requirements.txt"}),
    ("edit_file", {"path": "requirements.txt", "package": "requests", "version": "2.31.0"}),
    ("run_tests", {}),
    ("run_scan", {}),
]


class World:
    """What the fake scanner and fake test runner report."""

    def __init__(self):
        self.fixed_version = "2.31.0"  # None means 'no fixed version exists'
        self.tests_green = True


@pytest.fixture
def world(monkeypatch):
    w = World()

    def fake_scan(args, ctx):
        req = (ctx.repo_root / "requirements.txt").read_text()
        vulns = [] if "requests==2.25.1" not in req else [dict(VULN, fixed=w.fixed_version)]
        return json.dumps({"path": ".", "clean": not vulns, "count": len(vulns), "vulnerabilities": vulns})

    def fake_tests(args, ctx):
        green = w.tests_green
        return json.dumps({"green": green, "exit_code": 0 if green else 1, "passed": 3 if green else 2,
                           "failed": 0 if green else 1, "errors": 0, "summary": "", "output_tail": ""})

    tools = dict(registry.TOOLS)
    tools["run_scan"] = replace(tools["run_scan"], run=fake_scan)
    tools["run_tests"] = replace(tools["run_tests"], run=fake_tests)
    monkeypatch.setattr(registry, "TOOLS", tools)
    return w


class Approver:
    def __init__(self, answer=True):
        self.answer = answer
        self.proposals = []

    def __call__(self, proposal):
        self.proposals.append(proposal)
        return self.answer


@pytest.fixture
def work(tmp_path):
    if shutil.which("git") is None:
        pytest.skip("git not installed")
    return sandbox.reset(TEMPLATE, tmp_path / "work")


@pytest.fixture
def make_ctx(work):
    def make(answer=True):
        return ToolContext(repo_root=work, test_python=sys.executable, install_deps=False, approve=Approver(answer))
    return make


@pytest.fixture
def logger(tmp_path):
    return CallLogger(run_id="t", log_dir=tmp_path / "logs")


def tool_records(logger):
    return [r for r in logger.read() if "tool" in r and "event" not in r]


def requirements(ctx):
    return (ctx.repo_root / "requirements.txt").read_text()


# --- happy path ---------------------------------------------------------------

def test_fix_run_ends_done_and_commits_on_the_fix_branch(world, make_ctx, logger):
    ctx = make_ctx(True)
    result = run(ScriptedModel(FIX), ctx, logger=logger)

    assert result.stop_reason is StopReason.DONE
    assert result.state.steps == 5
    assert requirements(ctx).strip() == "requests==2.31.0"
    assert len(ctx.approve.proposals) == 1 and "+requests==2.31.0" in ctx.approve.proposals[0]["diff"]

    assert result.commit
    log = subprocess.run(["git", "log", "--format=%s", "patchpilot/fix"], cwd=ctx.repo_root,
                         capture_output=True, text=True).stdout
    assert log.splitlines()[0].startswith("PatchPilot:")
    main_req = subprocess.run(["git", "show", "main:requirements.txt"], cwd=ctx.repo_root,
                              capture_output=True, text=True).stdout
    assert main_req.strip() == "requests==2.25.1"  # baseline branch untouched


def test_trajectory_is_logged_with_a_named_stop_reason(world, make_ctx, logger):
    run(ScriptedModel(FIX), make_ctx(True), logger=logger)
    records = logger.read()
    assert records[0]["event"] == "start"
    assert [r["tool"] for r in tool_records(logger)] == [name for name, _ in FIX]
    assert [r["step"] for r in tool_records(logger)] == [1, 2, 3, 4, 5]
    assert sum(1 for r in records if r.get("event") == "model") == 5
    assert records[-1]["event"] == "stop" and records[-1]["reason"] == "done"


def test_tool_results_are_sent_back_to_the_model(world, make_ctx, logger):
    model = ScriptedModel(FIX)
    run(model, make_ctx(True), logger=logger)
    second_turn = model.seen[1]
    assert second_turn[-2]["tool_calls"][0]["function"]["name"] == "run_scan"
    assert second_turn[-1]["role"] == "tool" and '"clean": false' in second_turn[-1]["content"]


def test_already_clean_repo_is_done_without_any_edit(world, make_ctx, logger, work):
    (work / "requirements.txt").write_text("requests==2.31.0\n")
    ctx = make_ctx(True)
    result = run(ScriptedModel([("run_scan", {}), ("run_tests", {})]), ctx, logger=logger)
    assert result.stop_reason is StopReason.DONE
    assert result.state.steps == 2 and result.commit is None
    assert ctx.approve.proposals == []


# --- each stop reason ---------------------------------------------------------

def test_denied_approval_stops_the_run_and_writes_nothing(world, make_ctx, logger):
    ctx = make_ctx(False)
    result = run(ScriptedModel(FIX), ctx, logger=logger)
    assert result.stop_reason is StopReason.APPROVAL_DENIED
    assert result.state.steps == 3  # scan, read, denied edit; nothing after
    assert requirements(ctx).strip() == "requests==2.25.1"
    assert result.commit is None


def test_no_fixed_version_stops_after_the_scan(world, make_ctx, logger):
    world.fixed_version = None
    result = run(ScriptedModel(FIX), make_ctx(True), logger=logger)
    assert result.stop_reason is StopReason.NO_FIXED_VERSION
    assert result.state.steps == 1


def test_three_failed_fixes_stop_the_run(world, make_ctx, logger):
    world.tests_green = False
    script = []
    for version in ("2.26.0", "2.27.0", "2.28.0", "2.29.0"):
        script += [("edit_file", {"path": "requirements.txt", "package": "requests", "version": version}),
                   ("run_tests", {})]
    result = run(ScriptedModel(script), make_ctx(True), logger=logger)
    assert result.stop_reason is StopReason.FIX_ATTEMPTS
    assert result.state.failed_fixes == 3 and result.state.steps == 6


def test_a_model_that_never_finishes_halts_at_the_step_limit(world, make_ctx, logger):
    model = ScriptedModel([("read_file", {"path": "requirements.txt"})])  # repeats forever
    result = run(model, make_ctx(True), logger=logger)
    assert result.stop_reason is StopReason.STEP_LIMIT
    assert result.state.steps == 12
    assert model.calls == 12
    assert len(tool_records(logger)) == 12


def test_a_model_that_never_calls_a_tool_still_halts(world, make_ctx, logger):
    model = ScriptedModel([ModelReply(text="I think we are done.")])
    result = run(model, make_ctx(True), logger=logger)
    assert result.stop_reason is StopReason.STEP_LIMIT
    assert model.calls == 12 and tool_records(logger) == []
    assert model.seen[1][-1] == {"role": "user", "content": NUDGE}


def test_cost_cap_blocks_the_tool_call_that_would_exceed_it(world, make_ctx, logger):
    model = ScriptedModel([("read_file", {"path": "requirements.txt"})], cost_per_call=0.20)
    result = run(model, make_ctx(True), logger=logger)
    assert result.stop_reason is StopReason.COST_CAP
    assert model.calls == 3                  # $0.20, $0.40, $0.60
    assert len(tool_records(logger)) == 2    # the third call's tool never ran
    assert result.state.cost_usd == pytest.approx(0.60)


@pytest.mark.parametrize("max_steps", [1, 3, 7])
def test_step_limit_holds_for_any_limit(world, make_ctx, logger, max_steps):
    model = ScriptedModel([("run_scan", {})])
    result = run(model, make_ctx(True), limits=Limits(max_steps=max_steps), logger=logger)
    assert result.stop_reason is StopReason.STEP_LIMIT
    assert model.calls == max_steps == len(tool_records(logger))


# --- bad model output ---------------------------------------------------------

def test_bad_arguments_come_back_as_text_and_the_run_continues(world, make_ctx, logger):
    bad = ModelReply(text=None, tool_calls=[ToolCall("c1", "edit_file", '{"path": "requirements.txt"')])
    model = ScriptedModel([bad, ("read_file", {"path": "nope/../../x"}), *FIX])
    result = run(model, make_ctx(True), logger=logger)
    first, second = tool_records(logger)[:2]
    assert first["ok"] is False and first["error_type"] == "bad_arguments"
    assert second["ok"] is False and second["error_type"] == "not_allowed"
    assert "Traceback" not in model.seen[1][-1]["content"]
    assert result.stop_reason is StopReason.DONE


def test_unknown_tool_is_reported_not_raised(world, make_ctx, logger):
    model = ScriptedModel([("delete_repo", {}), *FIX])
    result = run(model, make_ctx(True), logger=logger)
    assert "unknown tool 'delete_repo'" in model.seen[1][-1]["content"]
    assert result.stop_reason is StopReason.DONE


def test_only_the_first_of_several_tool_calls_runs(world, make_ctx, logger):
    two = ModelReply(text=None, tool_calls=[ToolCall("a", "run_scan", "{}"),
                                            ToolCall("b", "edit_file", json.dumps(FIX[2][1]))])
    ctx = make_ctx(True)
    run(ScriptedModel([two, ModelReply(text="stop")]), ctx, limits=Limits(max_steps=2), logger=logger)
    assert [r["tool"] for r in tool_records(logger)] == ["run_scan"]
    assert ctx.approve.proposals == []
