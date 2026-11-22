"""open_pr against a fake GitHub (no network)."""

import base64
import json
import shutil
import sys

import pytest

from agent import sandbox
from agent.calllog import CallLogger
from agent.checkpoint import render_pr
from agent.stop_rule import RunState
from agent.tools import open_pr
from agent.tools.context import ToolContext
from agent.tools.registry import TOOLS, call_tool, tool_definitions
from tests.conftest import TEMPLATE

TOKEN = "ghp_test_token_value"
BASELINE = "requests==2.25.1\n"
FIXED = "requests==2.31.0\n"


class Approver:
    def __init__(self, answer=True):
        self.answer = answer
        self.proposals = []

    def __call__(self, proposal):
        self.proposals.append(proposal)
        return self.answer


class FakeGitHub:
    """Routes (METHOD, path without query) to canned (status, payload); records every call."""

    def __init__(self, routes):
        self.routes = dict(routes)
        self.calls = []

    def __call__(self, method, url, headers, body, timeout):
        path = url.split("api.github.com", 1)[1]
        self.calls.append({"method": method, "path": path, "headers": headers, "body": body})
        key = (method, path.split("?", 1)[0])
        if key not in self.routes:
            raise AssertionError(f"unexpected GitHub call {key}")
        r = self.routes[key]
        return r(body) if callable(r) else r

    def methods(self):
        return [(c["method"], c["path"].split("?", 1)[0]) for c in self.calls]

    def writes(self):
        return [c for c in self.calls if c["method"] != "GET"]


R = "/repos/o/r"


def routes(remote=BASELINE, open_prs=(), fix_branch_exists=False):
    return {
        ("GET", f"{R}/pulls"): (200, list(open_prs)),
        ("GET", f"{R}/contents/requirements.txt"): (200, {
            "encoding": "base64", "sha": "blob1", "content": base64.b64encode(remote.encode()).decode()}),
        ("GET", f"{R}/git/ref/heads/main"): (200, {"object": {"sha": "mainsha"}}),
        ("GET", f"{R}/git/ref/heads/patchpilot/fix"): (200, {"object": {"sha": "old"}}) if fix_branch_exists
        else (404, {"message": "Not Found"}),
        ("POST", f"{R}/git/refs"): (201, {"ref": "refs/heads/patchpilot/fix"}),
        ("PATCH", f"{R}/git/refs/heads/patchpilot/fix"): (200, {}),
        ("PUT", f"{R}/contents/requirements.txt"): (200, {"commit": {"sha": "headsha"}}),
        ("POST", f"{R}/pulls"): lambda body: (201, {"number": 12, "html_url": "https://github.com/o/r/pull/12",
                                                    "title": body["title"], "head": {"ref": "patchpilot/fix"}}),
    }


@pytest.fixture
def work(tmp_path):
    if shutil.which("git") is None:
        pytest.skip("git not installed")
    w = sandbox.reset(TEMPLATE, tmp_path / "work")
    (w / "requirements.txt").write_text(FIXED)
    return w


@pytest.fixture(autouse=True)
def token(monkeypatch):
    monkeypatch.setenv("GITHUB_TOKEN", TOKEN)


def ready_state(**kw):
    kw.setdefault("tests_green", True)
    kw.setdefault("scan_clean", True)
    kw.setdefault("found_vulns", ["CVE-2023-32681", "CVE-2024-35195"])
    kw.setdefault("tracking_issue", 5)
    return RunState(**kw)


def make_ctx(work, http, state=None, **kw):
    kw.setdefault("approve", Approver(True))
    kw.setdefault("issue_mode", "github")
    kw.setdefault("github_repo", "o/r")
    ctx = ToolContext(repo_root=work, test_python=sys.executable, install_deps=False, http=http, **kw)
    ctx.state = ready_state() if state is None else state
    return ctx


def test_opens_a_pr_with_only_requirements_and_fixes_the_tracking_issue(work):
    gh = FakeGitHub(routes())
    ctx = make_ctx(work, gh)
    res = call_tool("open_pr", {}, ctx)
    assert res.ok, res.content
    out = json.loads(res.content)
    assert out == {"opened": True, "existing": False, "number": 12, "url": "https://github.com/o/r/pull/12",
                   "head_sha": "headsha", "title": out["title"]}
    assert "CVE-2023-32681" in out["title"] and "requests" in out["title"]

    assert gh.methods() == [
        ("GET", f"{R}/pulls"), ("GET", f"{R}/contents/requirements.txt"),
        ("GET", f"{R}/git/ref/heads/main"), ("GET", f"{R}/git/ref/heads/patchpilot/fix"),
        ("POST", f"{R}/git/refs"), ("PUT", f"{R}/contents/requirements.txt"), ("POST", f"{R}/pulls"),
    ]
    assert gh.calls[4]["body"] == {"ref": "refs/heads/patchpilot/fix", "sha": "mainsha"}
    put = gh.calls[5]["body"]
    assert put["branch"] == "patchpilot/fix" and put["sha"] == "blob1"
    assert base64.b64decode(put["content"]).decode() == FIXED
    pr = gh.calls[6]["body"]
    assert pr["head"] == "patchpilot/fix" and pr["base"] == "main"
    assert "Fixes #5" in pr["body"] and "| requests | 2.25.1 | 2.31.0 |" in pr["body"]
    assert pr["body"].endswith(open_pr.FOOTER)

    proposal = ctx.approve.proposals[0]
    assert proposal["tool"] == "open_pr" and proposal["body"] == pr["body"] and proposal["title"] == pr["title"]
    assert "-requests==2.25.1" in proposal["diff"] and "+requests==2.31.0" in proposal["diff"]


def test_no_tracking_issue_means_no_fixes_line(work):
    gh = FakeGitHub(routes())
    res = call_tool("open_pr", {}, make_ctx(work, gh, ready_state(tracking_issue=None)))
    assert res.ok
    assert "Fixes #" not in gh.calls[-1]["body"]["body"]


def test_token_only_in_the_auth_header_never_in_url_result_or_log(work, tmp_path):
    gh = FakeGitHub(routes())
    logger = CallLogger(run_id="t", log_dir=tmp_path / "logs")
    res = call_tool("open_pr", {"notes": "Bumped to the smallest fixed version."}, make_ctx(work, gh), logger)
    assert res.ok
    for c in gh.calls:
        assert TOKEN not in c["path"] and TOKEN not in json.dumps(c["body"])
        assert c["headers"]["Authorization"] == f"Bearer {TOKEN}"
    assert TOKEN not in res.content
    assert TOKEN not in (tmp_path / "logs" / "t.jsonl").read_text()
    git_config = (work / ".git" / "config").read_text()
    assert TOKEN not in git_config and "github.com" not in git_config


@pytest.mark.parametrize("state", [
    RunState(tests_green=False, scan_clean=True),
    RunState(tests_green=True, scan_clean=False),
    None,
], ids=["tests-red", "scan-dirty", "no-run-state"])
def test_refuses_unless_tests_green_and_scan_clean_in_this_run(work, state):
    gh = FakeGitHub(routes())
    ctx = make_ctx(work, gh)
    ctx.state = state
    res = call_tool("open_pr", {}, ctx)
    assert not res.ok and res.error_type == "not_ready" and "Nothing was changed" in res.content
    assert gh.calls == [] and ctx.approve.proposals == []


def test_refuses_in_local_mode(work):
    gh = FakeGitHub(routes())
    res = call_tool("open_pr", {}, make_ctx(work, gh, issue_mode="local", github_repo=None))
    assert not res.ok and res.error_type == "not_allowed" and gh.calls == []


def test_refuses_on_drift_before_asking(work):
    gh = FakeGitHub(routes(remote="requests==2.28.0\n"))
    ctx = make_ctx(work, gh)
    res = call_tool("open_pr", {}, ctx)
    assert not res.ok and res.error_type == "drift" and "Nothing was changed" in res.content
    assert gh.writes() == [] and ctx.approve.proposals == []


def test_unchanged_requirements_is_refused(work):
    (work / "requirements.txt").write_text(BASELINE)
    gh = FakeGitHub(routes())
    res = call_tool("open_pr", {}, make_ctx(work, gh))
    assert not res.ok and res.error_type == "not_ready" and gh.calls == []


def test_existing_open_pr_is_returned_not_duplicated(work):
    existing = {"number": 7, "html_url": "u7", "title": "PatchPilot: old", "head": {"ref": "patchpilot/fix", "sha": "s7"}}
    gh = FakeGitHub(routes(open_prs=[existing]))
    ctx = make_ctx(work, gh)
    out = json.loads(call_tool("open_pr", {}, ctx).content)
    assert out["existing"] is True and out["opened"] is False and out["number"] == 7 and out["head_sha"] == "s7"
    assert gh.writes() == [] and ctx.approve.proposals == []


def test_existing_fix_branch_is_moved_to_main(work):
    gh = FakeGitHub(routes(fix_branch_exists=True))
    assert call_tool("open_pr", {}, make_ctx(work, gh)).ok
    patch = [c for c in gh.calls if c["method"] == "PATCH"][0]
    assert patch["body"] == {"sha": "mainsha", "force": True}
    assert ("POST", f"{R}/git/refs") not in gh.methods()


def test_denied_pushes_nothing(work):
    gh = FakeGitHub(routes())
    res = call_tool("open_pr", {}, make_ctx(work, gh, approve=Approver(False)))
    assert not res.ok and res.error_type == "approval_denied" and "Stop the run" in res.content
    assert gh.writes() == []


def test_no_approver_pushes_nothing(work):
    gh = FakeGitHub(routes())
    res = call_tool("open_pr", {}, make_ctx(work, gh, approve=None))
    assert not res.ok and res.error_type == "approval_required" and gh.writes() == []


def test_failed_pr_after_commit_says_what_happened(work):
    r = routes()
    r[("POST", f"{R}/pulls")] = (422, {"message": "Validation Failed"})
    res = call_tool("open_pr", {}, make_ctx(work, FakeGitHub(r)))
    assert not res.ok and "was committed on branch patchpilot/fix" in res.content
    assert "Nothing was changed" not in res.content


def test_strict_schema(work):
    res = call_tool("open_pr", {"branch": "main"}, make_ctx(work, FakeGitHub(routes())))
    assert not res.ok and res.error_type == "bad_arguments"


def test_registered_as_write_tool_and_hidden_in_local_mode():
    assert TOOLS["open_pr"].writes is True
    assert TOOLS["open_pr"].input_schema["additionalProperties"] is False
    assert "open_pr" not in [t["name"] for t in tool_definitions()]
    assert "open_pr" in [t["name"] for t in tool_definitions(github_mode=True)]


def test_checkpoint_shows_repo_branches_title_body_and_diff():
    text = render_pr({"tool": "open_pr", "repo": "o/r", "base": "main", "head": "patchpilot/fix",
                      "title": "PatchPilot: fix CVE-1\x1b[2J", "body": "Fixes #5", "fixes_issue": 5,
                      "diff": "-requests==2.25.1\n+requests==2.31.0"})
    assert "o/r" in text and "patchpilot/fix -> main" in text and "closes issue #5" in text
    assert "Title: PatchPilot: fix CVE-1" in text and "+requests==2.31.0" in text
    assert "\x1b" not in text
