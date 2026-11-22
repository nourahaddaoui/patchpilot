"""merge_pr against a fake GitHub and a fake clock (no network, no waiting)."""

import json
import shutil
import sys

import pytest

from agent import sandbox
from agent.checkpoint import render_merge
from agent.stop_rule import RunState
from agent.tools import merge_pr
from agent.tools.context import ToolContext
from agent.tools.registry import TOOLS, call_tool, tool_definitions
from tests.conftest import TEMPLATE
from tests.test_open_pr import Approver, FakeGitHub

R = "/repos/o/r"
SHA = "abc123def4567890"


def pr(merged=False, state="open", ref="patchpilot/fix"):
    return {"number": 12, "state": state, "merged": merged, "merge_commit_sha": "m1" if merged else None,
            "title": "PatchPilot: fix CVE-2023-32681 in requests", "html_url": "https://github.com/o/r/pull/12",
            "head": {"ref": ref, "sha": SHA}, "base": {"ref": "main"}}


def runs(*items):
    return (200, {"workflow_runs": [{"name": n, "status": s, "conclusion": c, "html_url": f"u/{n}"} for n, s, c in items]})


IN_PROGRESS = runs(("test", "in_progress", None))
PASSED = runs(("test", "completed", "success"))
FAILED = runs(("test", "completed", "failure"))


class Seq:
    """Returns the given responses in order, then repeats the last one."""

    def __init__(self, *responses):
        self.responses = list(responses)

    def __call__(self, body):
        return self.responses.pop(0) if len(self.responses) > 1 else self.responses[0]


def routes(the_pr=None, ci=PASSED, merge=(200, {"merged": True, "sha": "squashsha"})):
    return {
        ("GET", f"{R}/pulls/12"): (200, the_pr or pr()),
        ("GET", f"{R}/actions/runs"): ci if callable(ci) else Seq(ci),
        ("PUT", f"{R}/pulls/12/merge"): merge,
    }


class Clock:
    def __init__(self):
        self.t = 0.0
        self.sleeps = []

    def now(self):
        return self.t

    def sleep(self, s):
        self.sleeps.append(s)
        self.t += s


@pytest.fixture(autouse=True)
def clock(monkeypatch):
    c = Clock()
    monkeypatch.setattr(merge_pr, "_now", c.now)
    monkeypatch.setattr(merge_pr, "_sleep", c.sleep)
    monkeypatch.setenv("GITHUB_TOKEN", "t")
    return c


@pytest.fixture
def work(tmp_path):
    if shutil.which("git") is None:
        pytest.skip("git not installed")
    return sandbox.reset(TEMPLATE, tmp_path / "work")


def make_ctx(work, gh, state=None, **kw):
    kw.setdefault("approve", Approver(True))
    ctx = ToolContext(repo_root=work, test_python=sys.executable, install_deps=False, http=gh,
                      issue_mode="github", github_repo="o/r", **kw)
    ctx.state = state or RunState(tests_green=True, scan_clean=True, pr_number=12, pr_opened=True)
    return ctx


def test_waits_for_ci_then_squash_merges_the_tested_commit(work, clock):
    gh = FakeGitHub(routes(ci=Seq(IN_PROGRESS, IN_PROGRESS, PASSED)))
    ctx = make_ctx(work, gh)
    res = call_tool("merge_pr", {"number": 12}, ctx)
    assert res.ok, res.content
    out = json.loads(res.content)
    assert out["merged"] is True and out["already_merged"] is False and out["sha"] == "squashsha"
    assert clock.sleeps == [10, 10]
    put = gh.calls[-1]
    assert put["method"] == "PUT" and put["body"] == {"merge_method": "squash", "sha": SHA}
    assert f"head_sha={SHA}" in gh.calls[1]["path"]
    proposal = ctx.approve.proposals[0]
    assert proposal["number"] == 12 and proposal["head_sha"] == SHA and proposal["ci"][0]["conclusion"] == "success"
    # only PR, Actions-read and merge endpoints: nothing that needs an admin token
    assert {c["path"].split("?")[0] for c in gh.calls} == {f"{R}/pulls/12", f"{R}/actions/runs", f"{R}/pulls/12/merge"}


def test_ci_failure_is_refused_and_nothing_merged(work):
    gh = FakeGitHub(routes(ci=FAILED))
    ctx = make_ctx(work, gh)
    res = call_tool("merge_pr", {"number": 12}, ctx)
    assert not res.ok and res.error_type == "ci_failed" and "test: failure" in res.content
    assert "Nothing was merged" in res.content
    assert gh.writes() == [] and ctx.approve.proposals == []


def test_ci_timeout_is_bounded_and_refused(work, clock):
    gh = FakeGitHub(routes(ci=IN_PROGRESS))
    ctx = make_ctx(work, gh)
    res = call_tool("merge_pr", {"number": 12}, ctx)
    assert not res.ok and res.error_type == "ci_timeout" and "180s" in res.content
    assert sum(clock.sleeps) == 180 and set(clock.sleeps) == {10}
    assert gh.writes() == [] and ctx.approve.proposals == []


def test_no_ci_run_at_all_times_out(work, clock):
    gh = FakeGitHub(routes(ci=(200, {"workflow_runs": []})))
    res = call_tool("merge_pr", {"number": 12}, make_ctx(work, gh))
    assert not res.ok and res.error_type == "ci_timeout" and gh.writes() == []


def test_already_merged_is_reported_and_nothing_else_happens(work):
    gh = FakeGitHub(routes(the_pr=pr(merged=True, state="closed")))
    ctx = make_ctx(work, gh)
    out = json.loads(call_tool("merge_pr", {"number": 12}, ctx).content)
    assert out == {"merged": True, "already_merged": True, "number": 12, "sha": "m1", "ci": None}
    assert len(gh.calls) == 1 and ctx.approve.proposals == []


@pytest.mark.parametrize("state, kind", [
    (RunState(tests_green=True, scan_clean=True, pr_number=12, pr_opened=False), "not_allowed"),  # older PR
    (RunState(tests_green=True, scan_clean=True, pr_number=13, pr_opened=True), "not_allowed"),   # other PR
    (RunState(tests_green=False, scan_clean=True, pr_number=12, pr_opened=True), "not_ready"),
    (RunState(tests_green=True, scan_clean=False, pr_number=12, pr_opened=True), "not_ready"),
], ids=["not-opened-this-run", "other-number", "tests-red", "scan-dirty"])
def test_refuses_without_touching_github(work, state, kind):
    gh = FakeGitHub(routes())
    res = call_tool("merge_pr", {"number": 12}, make_ctx(work, gh, state))
    assert not res.ok and res.error_type == kind and gh.calls == []


def test_refuses_a_pr_from_another_branch(work):
    gh = FakeGitHub(routes(the_pr=pr(ref="feature/x")))
    res = call_tool("merge_pr", {"number": 12}, make_ctx(work, gh))
    assert not res.ok and res.error_type == "not_allowed" and gh.writes() == []


def test_closed_unmerged_pr_is_refused(work):
    gh = FakeGitHub(routes(the_pr=pr(state="closed")))
    res = call_tool("merge_pr", {"number": 12}, make_ctx(work, gh))
    assert not res.ok and "closed without being merged" in res.content and gh.writes() == []


def test_denied_merges_nothing(work):
    gh = FakeGitHub(routes())
    res = call_tool("merge_pr", {"number": 12}, make_ctx(work, gh, approve=Approver(False)))
    assert not res.ok and res.error_type == "approval_denied" and gh.writes() == []


@pytest.mark.parametrize("status, text", [(405, "cannot be merged yet"), (409, "changed after CI ran")])
def test_github_refusing_the_merge_is_plain_text(work, status, text):
    gh = FakeGitHub(routes(merge=(status, {"message": "x"})))
    res = call_tool("merge_pr", {"number": 12}, make_ctx(work, gh))
    assert not res.ok and text in res.content and "Traceback" not in res.content


def test_ci_status_rules():
    run = lambda s, c: {"status": s, "conclusion": c}  # noqa: E731
    assert merge_pr.ci_status([]) == "pending"
    assert merge_pr.ci_status([run("completed", "success"), run("queued", None)]) == "pending"
    assert merge_pr.ci_status([run("completed", "success"), run("completed", "skipped")]) == "passed"
    assert merge_pr.ci_status([run("completed", "skipped")]) == "failed"  # nothing actually passed
    assert merge_pr.ci_status([run("completed", "success"), run("completed", "cancelled")]) == "failed"


@pytest.mark.parametrize("args", [{}, {"number": "12"}, {"number": 0}, {"number": 12, "method": "merge"}])
def test_strict_schema(work, args):
    res = call_tool("merge_pr", args, make_ctx(work, FakeGitHub(routes())))
    assert not res.ok and res.error_type == "bad_arguments"


def test_registered_as_write_tool_and_hidden_in_local_mode():
    assert TOOLS["merge_pr"].writes is True
    assert "merge_pr" not in [t["name"] for t in tool_definitions()]
    assert "merge_pr" in [t["name"] for t in tool_definitions(github_mode=True)]


def test_checkpoint_shows_pr_ci_and_what_gets_merged():
    text = render_merge({"tool": "merge_pr", "repo": "o/r", "number": 12, "title": "PatchPilot: fix\x1b[2J",
                         "head": "patchpilot/fix", "base": "main", "head_sha": SHA, "url": "u",
                         "ci": [{"name": "test", "status": "completed", "conclusion": "success"}]})
    assert "PR #12" in text and "o/r" in text and SHA[:12] in text and "-> main" in text
    assert "completed/success" in text and "\x1b" not in text
