"""create_issue inside the loop, the stop rule and the checkpoint."""

import json

from agent.checkpoint import Checkpoint, ScriptedHuman, render_issue
from agent.loop import run
from agent.model import ScriptedModel
from agent.stop_rule import RunState, StopReason, check
from agent.tools import create_issue
from tests.test_loop import FIX, logger, make_ctx, tool_records, work, world  # noqa: F401  (fixtures)

ISSUE = ("create_issue", {"title": "No fixed version for requests", "body": "CVE-2023-32681 has no fix yet."})


def issues_in(work):
    return sorted((work / create_issue.STORE).glob("*.json"))


# --- stop rule ----------------------------------------------------------------------

def test_after_no_fix_exactly_one_issue_call_is_allowed():
    s = RunState(no_fixed_version=True)
    assert check(s, next_tool="create_issue") is None
    assert check(s, next_tool="read_file") is StopReason.NO_FIXED_VERSION
    assert check(s) is StopReason.NO_FIXED_VERSION
    s.issue_filed = True
    assert check(s, next_tool="create_issue") is StopReason.NO_FIXED_VERSION


def test_a_filed_issue_ends_the_run_as_handed_off():
    assert check(RunState(issue_filed=True)) is StopReason.HANDED_OFF
    assert check(RunState(issue_filed=True, scan_clean=True, tests_green=True)) is StopReason.DONE
    assert check(RunState(issue_filed=True, approval_denied=True)) is StopReason.APPROVAL_DENIED


# --- loop -----------------------------------------------------------------------------

def test_no_fixed_version_files_one_issue_then_stops(world, make_ctx, logger, work):
    world.fixed_version = None
    ctx = make_ctx(True)
    result = run(ScriptedModel([("run_scan", {}), ISSUE, ISSUE]), ctx, logger=logger)
    assert result.stop_reason is StopReason.NO_FIXED_VERSION
    assert [r["tool"] for r in tool_records(logger)] == ["run_scan", "create_issue"]
    assert result.state.issue_filed and len(issues_in(work)) == 1
    assert ctx.approve.proposals[0]["tool"] == "create_issue"


def test_denied_issue_stops_the_run_and_files_nothing(world, make_ctx, logger, work):
    world.fixed_version = None
    result = run(ScriptedModel([("run_scan", {}), ISSUE]), make_ctx(False), logger=logger)
    assert result.stop_reason is StopReason.APPROVAL_DENIED
    assert issues_in(work) == []


def test_broken_fix_handed_off_with_an_issue(world, make_ctx, logger, work):
    world.tests_green = False
    script = FIX[:4] + [("create_issue", {"title": "Bumping requests breaks the tests", "body": "1 test fails."}),
                        ("run_scan", {})]
    result = run(ScriptedModel(script), make_ctx(True), logger=logger)
    assert result.stop_reason is StopReason.HANDED_OFF
    assert [r["tool"] for r in tool_records(logger)][-1] == "create_issue"
    assert result.state.failed_fixes == 1 and result.commit is None
    assert len(issues_in(work)) == 1


# --- checkpoint ---------------------------------------------------------------------

PROPOSAL = {"tool": "create_issue", "mode": "local", "repo": None,
            "title": "No fixed version\x1b[31m for requests", "body": "line one\nline two"}


def test_issue_checkpoint_shows_title_and_body_and_logs_the_yes(work, logger):
    from agent.tools.context import ToolContext
    shown, human = [], ScriptedHuman(["y"])
    cp = Checkpoint(ToolContext(repo_root=work), logger, ask=human, show=shown.append)
    assert cp(PROPOSAL) is True
    text = shown[0]
    assert "local issue store" in text and "line one\nline two" in text
    assert "\x1b" not in text  # control characters from untrusted text are stripped
    decision = [r for r in logger.read() if r.get("event") == "checkpoint"][0]
    assert decision["approved"] is True and decision["proposal"]["tool"] == "create_issue"


def test_issue_checkpoint_no_input_means_no(work):
    from agent.tools.context import ToolContext
    cp = Checkpoint(ToolContext(repo_root=work), None, ask=ScriptedHuman([]), show=lambda s: None)
    assert cp(PROPOSAL) is False


def test_github_target_is_named_before_approval():
    text = render_issue({**PROPOSAL, "mode": "github", "repo": "o/r"})
    assert "GitHub repo o/r" in text


# --- tracking issue (GitHub mode) ----------------------------------------------------

def test_tracking_issue_does_not_end_the_run():
    assert check(RunState(tracking_issue=5)) is None
    assert check(RunState(tracking_issue=5, issue_filed=True)) is StopReason.HANDED_OFF


def test_loop_keeps_going_after_a_tracking_issue_and_remembers_its_number(world, work, logger, monkeypatch):
    import sys
    from agent.stop_rule import Limits
    from agent.tools.context import ToolContext
    from tests.test_create_issue import Approver, FakeHTTP
    monkeypatch.setenv("GITHUB_TOKEN", "t")
    http = FakeHTTP((200, []), (201, {"number": 9, "html_url": "https://github.com/o/r/issues/9"}))
    ctx = ToolContext(repo_root=work, test_python=sys.executable, install_deps=False, approve=Approver(True),
                      issue_mode="github", github_repo="o/r", http=http)
    track = ("create_issue", {"title": "Track: requests CVE-2023-32681", "body": "Tracking the fix.",
                              "purpose": "tracking"})
    result = run(ScriptedModel([("run_scan", {}), track, ("read_file", {"path": "requirements.txt"})]),
                 ctx, limits=Limits(max_steps=3), logger=logger)
    assert [r["tool"] for r in tool_records(logger)] == ["run_scan", "create_issue", "read_file"]
    assert result.stop_reason is StopReason.STEP_LIMIT  # not handed_off
    assert result.state.tracking_issue == 9 and result.state.issue_filed is False


def test_issue_checkpoint_names_the_purpose():
    assert "Purpose: tracking" in render_issue({**PROPOSAL, "mode": "github", "repo": "o/r", "purpose": "tracking"})
    assert "Purpose: handoff" in render_issue(PROPOSAL)
