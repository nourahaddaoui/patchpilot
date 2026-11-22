"""End to end in GitHub mode: RulesModel + the real Checkpoint (ScriptedHuman) + a fake GitHub.

scan -> tracking issue -> read -> edit -> tests -> rescan -> open_pr -> merge_pr -> DONE.
Fake scanner and test runner come from tests/test_loop.py; no network, no waiting.
"""

import base64
import sys

import pytest

from agent.checkpoint import Checkpoint, ScriptedHuman
from agent.loop import GITHUB_PROMPT, SYSTEM_PROMPT, run
from agent.rules_model import RulesModel
from agent.stop_rule import StopReason
from agent.tools import merge_pr
from agent.tools.context import ToolContext
from tests.conftest import TEMPLATE
from tests.test_loop import logger, tool_records, work, world  # noqa: F401  (fixtures)
from tests.test_open_pr import FakeGitHub

R = "/repos/o/r"
FLOW = ["run_scan", "create_issue", "read_file", "edit_file", "run_tests", "run_scan", "open_pr", "merge_pr"]


def github_routes(ci_conclusion="success"):
    baseline = (TEMPLATE / "requirements.txt").read_text()
    return {
        ("GET", f"{R}/issues"): (200, []),
        ("POST", f"{R}/issues"): (201, {"number": 3, "html_url": "https://github.com/o/r/issues/3"}),
        ("GET", f"{R}/pulls"): (200, []),
        ("GET", f"{R}/contents/requirements.txt"): (200, {
            "encoding": "base64", "sha": "blob1", "content": base64.b64encode(baseline.encode()).decode()}),
        ("GET", f"{R}/git/ref/heads/main"): (200, {"object": {"sha": "mainsha"}}),
        ("GET", f"{R}/git/ref/heads/patchpilot/fix"): (404, {"message": "Not Found"}),
        ("POST", f"{R}/git/refs"): (201, {}),
        ("PUT", f"{R}/contents/requirements.txt"): (200, {"commit": {"sha": "headsha"}}),
        ("POST", f"{R}/pulls"): lambda body: (201, {"number": 12, "html_url": "https://github.com/o/r/pull/12",
                                                    "title": body["title"], "head": {"ref": "patchpilot/fix"}}),
        ("GET", f"{R}/pulls/12"): (200, {"number": 12, "state": "open", "merged": False, "title": "t",
                                         "head": {"ref": "patchpilot/fix", "sha": "headsha"}, "base": {"ref": "main"}}),
        ("GET", f"{R}/actions/runs"): (200, {"workflow_runs": [
            {"name": "test", "status": "completed", "conclusion": ci_conclusion}]}),
        ("PUT", f"{R}/pulls/12/merge"): (200, {"merged": True, "sha": "squashsha"}),
    }


@pytest.fixture(autouse=True)
def offline_github(monkeypatch):
    monkeypatch.setenv("GITHUB_TOKEN", "t")
    monkeypatch.setattr(merge_pr, "_sleep", lambda s: None)


def github_ctx(work, gh, answers):
    ctx = ToolContext(repo_root=work, test_python=sys.executable, install_deps=False,
                      issue_mode="github", github_repo="o/r", http=gh)
    human = ScriptedHuman(answers)
    ctx.approve = Checkpoint(ctx, None, ask=human, show=lambda s: None)
    return ctx, human


def test_full_github_flow_ends_done_after_merge(world, work, logger):
    gh = FakeGitHub(github_routes())
    ctx, human = github_ctx(work, gh, ["y", "y", "y", "y"])
    result = run(RulesModel(), ctx, logger=logger)

    assert [r["tool"] for r in tool_records(logger)] == FLOW
    assert result.stop_reason is StopReason.DONE and result.state.steps == 8
    assert result.state.tracking_issue == 3 and result.state.pr_number == 12 and result.state.merged
    assert result.state.issue_filed is False  # the tracking issue did not hand off

    # four writes, four approvals, in flow order
    assert human.prompts == 4
    assert [d["proposal"].get("tool") or d["proposal"].get("path") for d in ctx.approve.decisions] == \
        ["create_issue", "requirements.txt", "open_pr", "merge_pr"]
    assert all(d["approved"] for d in ctx.approve.decisions)

    issue = next(c for c in gh.calls if c["method"] == "POST" and c["path"] == f"{R}/issues")
    assert "CVE-2023-32681" in issue["body"]["title"]
    pr = next(c for c in gh.calls if c["method"] == "POST" and c["path"] == f"{R}/pulls")
    assert "Fixes #3" in pr["body"]["body"] and "CVE-2023-32681" in pr["body"]["title"]
    put = next(c for c in gh.calls if c["path"] == f"{R}/contents/requirements.txt" and c["method"] == "PUT")
    assert base64.b64decode(put["body"]["content"]).decode().strip() == "requests==2.31.0"
    assert gh.calls[-1]["path"] == f"{R}/pulls/12/merge"
    assert (work / "requirements.txt").read_text().strip() == "requests==2.31.0"

    system = result.messages[0]["content"]
    assert system == GITHUB_PROMPT and "merge_pr" in system


def test_denied_merge_stops_and_merges_nothing(world, work, logger):
    gh = FakeGitHub(github_routes())
    ctx, _ = github_ctx(work, gh, ["y", "y", "y", "n"])
    result = run(RulesModel(), ctx, logger=logger)
    assert result.stop_reason is StopReason.APPROVAL_DENIED
    assert [r["tool"] for r in tool_records(logger)] == FLOW
    assert not any(c["path"].endswith("/merge") for c in gh.calls)


def test_failed_ci_hands_off_with_an_issue(world, work, logger):
    gh = FakeGitHub(github_routes(ci_conclusion="failure"))
    ctx, _ = github_ctx(work, gh, ["y", "y", "y", "y"])
    result = run(RulesModel(), ctx, logger=logger)
    assert [r["tool"] for r in tool_records(logger)] == FLOW + ["create_issue"]
    assert result.stop_reason is StopReason.HANDED_OFF and not result.state.merged
    assert not any(c["path"].endswith("/merge") for c in gh.calls)


def test_local_mode_flow_and_prompt_unchanged(world, work, logger):
    from tests.test_loop import Approver
    ctx = ToolContext(repo_root=work, test_python=sys.executable, install_deps=False, approve=Approver(True))
    result = run(RulesModel(), ctx, logger=logger)
    assert [r["tool"] for r in tool_records(logger)] == ["run_scan", "read_file", "edit_file", "run_tests", "run_scan"]
    assert result.stop_reason is StopReason.DONE and result.commit
    assert result.messages[0]["content"] == SYSTEM_PROMPT
    assert "open_pr" not in SYSTEM_PROMPT
