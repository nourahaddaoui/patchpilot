import json
import shutil
import sys

import pytest

from agent import sandbox
from agent.tools import create_issue
from agent.tools.context import ToolContext
from agent.tools.registry import call_tool
from tests.conftest import TEMPLATE

ARGS = {"title": "CVE-2024-0001: no fixed version of requests", "body": "Trivy reports no fix. @alice please decide."}


class Approver:
    def __init__(self, answer=True):
        self.answer = answer
        self.proposals = []

    def __call__(self, proposal):
        self.proposals.append(proposal)
        return self.answer


class FakeHTTP:
    def __init__(self, *responses):
        self.responses = list(responses)
        self.calls = []

    def __call__(self, method, url, headers, body, timeout):
        self.calls.append({"method": method, "url": url, "headers": headers, "body": body})
        r = self.responses.pop(0)
        if isinstance(r, BaseException):
            raise r
        return r


@pytest.fixture
def work(tmp_path):
    if shutil.which("git") is None:
        pytest.skip("git not installed")
    return sandbox.reset(TEMPLATE, tmp_path / "work")


def make_ctx(repo, **kw):
    kw.setdefault("approve", Approver(True))
    kw.setdefault("issue_mode", "local")
    kw.setdefault("github_repo", None)
    return ToolContext(repo_root=repo, test_python=sys.executable, install_deps=False, **kw)


def test_local_issue_is_filed_after_approval_and_never_committed(work):
    ctx = make_ctx(work)
    res = call_tool("create_issue", ARGS, ctx)
    assert res.ok, res.content
    out = json.loads(res.content)
    assert out == {"filed": True, "existing": False, "number": 1, "url": None, "mode": "local",
                   "purpose": "handoff", "title": ARGS["title"]}
    proposal = ctx.approve.proposals[0]
    assert proposal["tool"] == "create_issue" and proposal["title"] == ARGS["title"]
    assert proposal["body"].endswith(create_issue.FOOTER)
    stored = list((work / create_issue.STORE).glob("*.json"))
    assert len(stored) == 1
    # lives inside .git, so the sandbox working tree stays clean
    import subprocess
    status = subprocess.run(["git", "status", "--porcelain"], cwd=work, capture_output=True, text=True).stdout
    assert status.strip() == ""


def test_same_title_twice_files_once_and_asks_once(work):
    ctx = make_ctx(work)
    call_tool("create_issue", ARGS, ctx)
    again = call_tool("create_issue", {**ARGS, "title": "  cve-2024-0001:  NO fixed version of requests "}, ctx)
    out = json.loads(again.content)
    assert again.ok and out["filed"] is False and out["existing"] is True and out["number"] == 1
    assert len(ctx.approve.proposals) == 1
    assert len(list((work / create_issue.STORE).glob("*.json"))) == 1


def test_denied_files_nothing(work):
    ctx = make_ctx(work, approve=Approver(False))
    res = call_tool("create_issue", ARGS, ctx)
    assert not res.ok and res.error_type == "approval_denied" and "Stop the run" in res.content
    assert not (work / create_issue.STORE).exists()


def test_no_approver_files_nothing(work):
    ctx = make_ctx(work, approve=None)
    res = call_tool("create_issue", ARGS, ctx)
    assert not res.ok and res.error_type == "approval_required"


def test_refuses_outside_sandbox(template_repo):
    ctx = make_ctx(template_repo)
    res = call_tool("create_issue", ARGS, ctx)
    assert not res.ok and res.error_type == "not_allowed"
    assert ctx.approve.proposals == []


def test_mentions_never_ping():
    assert create_issue.neutralize("ping @alice and @bob-2, mail a@b.com, `@ok`") == \
        "ping `@alice` and `@bob-2`, mail a@b.com, `@ok`"


@pytest.mark.parametrize("args, problem", [
    ({"title": "x", "body": "b"}, "too short"),
    ({"title": "two\nlines here", "body": "b"}, "invalid format"),
    ({"title": "fine title", "body": ""}, "must not be empty"),
    ({"title": "fine title", "body": "b", "labels": "x"}, "unexpected argument"),
])
def test_strict_schema(work, args, problem):
    res = call_tool("create_issue", args, make_ctx(work))
    assert not res.ok and res.error_type == "bad_arguments" and problem in res.content


# --- github mode (no network: fake transport) -----------------------------------

def test_github_mode_posts_after_approval(work, monkeypatch):
    monkeypatch.setenv("GITHUB_TOKEN", "t0ken")
    http = FakeHTTP((200, []), (201, {"number": 42, "html_url": "https://github.com/o/r/issues/42"}))
    ctx = make_ctx(work, issue_mode="github", github_repo="o/r", http=http)
    res = call_tool("create_issue", ARGS, ctx)
    out = json.loads(res.content)
    assert res.ok and out["number"] == 42 and out["url"].endswith("/42") and out["mode"] == "github"
    assert [c["method"] for c in http.calls] == ["GET", "POST"]
    post = http.calls[1]
    assert post["url"] == "https://api.github.com/repos/o/r/issues"
    assert "`@alice`" in post["body"]["body"]
    assert post["headers"]["Authorization"] == "Bearer t0ken"
    assert "t0ken" not in res.content


def test_github_existing_open_issue_is_not_duplicated(work, monkeypatch):
    monkeypatch.setenv("GITHUB_TOKEN", "t")
    http = FakeHTTP((200, [{"title": ARGS["title"], "number": 7, "html_url": "u7"}]))
    ctx = make_ctx(work, issue_mode="github", github_repo="o/r", http=http)
    out = json.loads(call_tool("create_issue", ARGS, ctx).content)
    assert out["existing"] is True and out["number"] == 7
    assert ctx.approve.proposals == [] and len(http.calls) == 1


def test_github_without_token_is_plain_error(work, monkeypatch):
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    ctx = make_ctx(work, issue_mode="github", github_repo="o/r", http=FakeHTTP())
    res = call_tool("create_issue", ARGS, ctx)
    assert not res.ok and res.error_type == "tool_down" and "GITHUB_TOKEN" in res.content


@pytest.mark.parametrize("status, kind", [(401, "not_allowed"), (404, "not_found"), (500, "tool_down")])
def test_github_http_errors(work, monkeypatch, status, kind):
    monkeypatch.setenv("GITHUB_TOKEN", "t")
    ctx = make_ctx(work, issue_mode="github", github_repo="o/r", http=FakeHTTP((status, {"message": "x"})))
    res = call_tool("create_issue", ARGS, ctx)
    assert not res.ok and res.error_type == kind and "Traceback" not in res.content


def test_github_unreachable(work, monkeypatch):
    monkeypatch.setenv("GITHUB_TOKEN", "t")
    ctx = make_ctx(work, issue_mode="github", github_repo="o/r", http=FakeHTTP(OSError("down")))
    res = call_tool("create_issue", ARGS, ctx)
    assert not res.ok and res.error_type == "tool_down"


def test_github_needs_repo(work, monkeypatch):
    monkeypatch.setenv("GITHUB_TOKEN", "t")
    res = call_tool("create_issue", ARGS, make_ctx(work, issue_mode="github", github_repo=None))
    assert not res.ok and "PATCHPILOT_GITHUB_REPO" in res.content


def test_archive_copy_survives_sandbox_reset(work, tmp_path):
    archive = tmp_path / "archive"
    ctx = make_ctx(work, issue_archive=archive)
    out = json.loads(call_tool("create_issue", ARGS, ctx).content)
    saved = list(archive.glob("*-0001.json"))
    assert len(saved) == 1 and out["saved_to"] == str(saved[0])
    sandbox.reset(TEMPLATE, work)  # wipes the sandbox store
    assert not (work / create_issue.STORE).exists()
    assert json.loads(saved[0].read_text())["title"] == ARGS["title"]


# --- purpose: handoff (default) vs tracking ----------------------------------------

TRACK = {**ARGS, "title": "Track: CVE-2023-32681 in requests", "purpose": "tracking"}


def test_tracking_is_refused_in_local_mode(work):
    ctx = make_ctx(work)
    res = call_tool("create_issue", TRACK, ctx)
    assert not res.ok and res.error_type == "not_allowed" and "Nothing was filed" in res.content
    assert ctx.approve.proposals == [] and not (work / create_issue.STORE).exists()


def test_tracking_in_github_mode_is_filed_and_marked(work, monkeypatch):
    monkeypatch.setenv("GITHUB_TOKEN", "t")
    http = FakeHTTP((200, []), (201, {"number": 5, "html_url": "https://github.com/o/r/issues/5"}))
    ctx = make_ctx(work, issue_mode="github", github_repo="o/r", http=http)
    out = json.loads(call_tool("create_issue", TRACK, ctx).content)
    assert out["filed"] is True and out["number"] == 5 and out["purpose"] == "tracking"
    assert ctx.approve.proposals[0]["purpose"] == "tracking"


def test_second_tracking_issue_in_a_run_returns_the_first(work, monkeypatch):
    from agent.stop_rule import RunState
    monkeypatch.setenv("GITHUB_TOKEN", "t")
    http = FakeHTTP()
    ctx = make_ctx(work, issue_mode="github", github_repo="o/r", http=http)
    ctx.state = RunState(tracking_issue=5)
    out = json.loads(call_tool("create_issue", {**TRACK, "title": "Another tracking title"}, ctx).content)
    assert out["existing"] is True and out["number"] == 5
    assert ctx.approve.proposals == [] and http.calls == []


def test_purpose_is_an_enum(work):
    res = call_tool("create_issue", {**ARGS, "purpose": "fyi"}, make_ctx(work))
    assert not res.ok and res.error_type == "bad_arguments" and "must be one of" in res.content
