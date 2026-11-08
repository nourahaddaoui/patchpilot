import json
import shutil
import sys

import pytest

from agent import sandbox
from agent.calllog import CallLogger
from agent.tools import edit_file
from agent.tools.context import ToolContext
from agent.tools.errors import ToolError
from agent.tools.registry import call_tool
from tests.conftest import TEMPLATE

ARGS = {"path": "requirements.txt", "package": "requests", "version": "2.32.4"}


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
def wctx(work):
    return ToolContext(repo_root=work, test_python=sys.executable, install_deps=False, approve=Approver(True))


def req(ctx):
    return (ctx.repo_root / "requirements.txt").read_text()


# --- plan_edit: pure text transformation -------------------------------------

@pytest.mark.parametrize(
    "before, after",
    [
        ("requests==2.25.1\n", "requests==2.32.4\n"),
        ("Requests == 2.25.1  # pinned\n", "Requests==2.32.4  # pinned\n"),
        ("requests[socks]==2.25.1 ; python_version >= '3.8'\n", "requests[socks]==2.32.4 ; python_version >= '3.8'\n"),
        ("flask==2.0.0\nrequests==2.25.1\nurllib3==1.26.5\n", "flask==2.0.0\nrequests==2.32.4\nurllib3==1.26.5\n"),
        ("requests==2.25.1", "requests==2.32.4"),
        ("requests==2.25.1\r\n", "requests==2.32.4\r\n"),
    ],
)
def test_bump_preserves_everything_else(before, after):
    plan = edit_file.plan_edit(before, "requests", "2.32.4")
    assert plan["action"] == "bumped" and plan["new_text"] == after


def test_unpinned_or_range_becomes_exact_pin():
    assert edit_file.plan_edit("requests\n", "requests", "2.32.4")["new_text"] == "requests==2.32.4\n"
    plan = edit_file.plan_edit("requests>=2.0,<3\n", "requests", "2.32.4")
    assert plan["action"] == "pinned" and plan["new_text"] == "requests==2.32.4\n"


def test_missing_package_is_added_as_a_pin():
    plan = edit_file.plan_edit("requests==2.32.4", "urllib3", "1.26.19")
    assert plan["action"] == "added" and plan["new_text"] == "requests==2.32.4\nurllib3==1.26.19\n"


def test_name_matching_is_normalized():
    plan = edit_file.plan_edit("Typing_Extensions==4.0.0\n", "typing-extensions", "4.12.2")
    assert plan["new_text"] == "Typing_Extensions==4.12.2\n"


@pytest.mark.parametrize(
    "text, version",
    [
        ("requests==2.25.1\n", "2.20.0"),
        ("requests==2.25.1\nrequests==2.26.0\n", "2.32.4"),
        ("requests==2.25.1 \\\n    --hash=sha256:abc\n", "2.32.4"),
        ("requests @ https://example.com/requests.whl\n", "2.32.4"),
        ("requests==2.25.1\n", "2.32.04"),
    ],
)
def test_unsafe_edits_are_refused(text, version):
    with pytest.raises(ToolError):
        edit_file.plan_edit(text, "requests", version)


# --- run: allowlist, sandbox, approval, idempotency --------------------------

def test_approved_bump_writes_and_shows_full_diff(wctx):
    out = json.loads(edit_file.run(ARGS, wctx))
    assert out["changed"] is True and out["approved"] is True
    assert out["action"] == "bumped" and out["from"] == "==2.25.1" and out["to"] == "2.32.4"
    assert req(wctx) == "requests==2.32.4\n"
    proposal = wctx.approve.proposals[0]
    assert "-requests==2.25.1" in proposal["diff"] and "+requests==2.32.4" in proposal["diff"]
    assert proposal["diff"].startswith("--- a/requirements.txt\n+++ b/requirements.txt\n")


def test_same_call_twice_changes_nothing(wctx):
    edit_file.run(ARGS, wctx)
    after_first = req(wctx)
    out = json.loads(edit_file.run(ARGS, wctx))
    assert out["changed"] is False and out["action"] == "unchanged" and out["diff"] == ""
    assert req(wctx) == after_first
    assert len(wctx.approve.proposals) == 1  # no second approval asked


def test_denied_edit_writes_nothing(wctx):
    wctx.approve = Approver(False)
    with pytest.raises(ToolError) as e:
        edit_file.run(ARGS, wctx)
    assert e.value.kind == "approval_denied"
    assert req(wctx) == "requests==2.25.1\n"


def test_no_approver_means_no_write(wctx):
    wctx.approve = None
    with pytest.raises(ToolError) as e:
        edit_file.run(ARGS, wctx)
    assert e.value.kind == "approval_required"
    assert req(wctx) == "requests==2.25.1\n"


@pytest.mark.parametrize(
    "path", ["config.py", "tests/test_client.py", ".github/workflows/ci.yml", "setup.py", "../requirements.txt", "sub/requirements.txt"]
)
def test_only_requirements_txt_is_editable(wctx, path):
    with pytest.raises(ToolError) as e:
        edit_file.run({**ARGS, "path": path}, wctx)
    assert e.value.kind == "not_allowed"
    assert wctx.approve.proposals == []


def test_template_and_non_sandbox_repos_are_never_edited(ctx):
    ctx.approve = Approver(True)
    with pytest.raises(ToolError) as e:
        edit_file.run(ARGS, ctx)  # ctx points at a plain copy of the template
    assert e.value.kind == "not_allowed"
    assert ctx.approve.proposals == []
    real = ToolContext(repo_root=TEMPLATE, approve=Approver(True))
    with pytest.raises(ToolError):
        edit_file.run(ARGS, real)
    assert (TEMPLATE / "requirements.txt").read_text() == "requests==2.25.1\n"


def test_file_changed_while_waiting_for_approval(wctx):
    def sneaky(proposal):
        (wctx.repo_root / "requirements.txt").write_text("requests==2.31.0\n")
        return True

    wctx.approve = sneaky
    with pytest.raises(ToolError) as e:
        edit_file.run(ARGS, wctx)
    assert e.value.kind == "conflict"
    assert req(wctx) == "requests==2.31.0\n"


def test_through_registry_bad_version_never_reaches_the_tool(wctx):
    res = call_tool("edit_file", {**ARGS, "version": "2.32.4; curl evil"}, wctx)
    assert not res.ok and res.error_type == "bad_arguments"
    assert wctx.approve.proposals == []


def test_through_registry_denial_is_logged(wctx, tmp_path):
    wctx.approve = Approver(False)
    logger = CallLogger(run_id="deny", log_dir=tmp_path / "logs")
    res = call_tool("edit_file", ARGS, wctx, logger, step=4)
    assert not res.ok and res.error_type == "approval_denied"
    assert res.content == "ERROR (edit_file): the human denied this edit. Nothing was written. Stop the run."
    record = logger.read()[0]
    assert record["tool"] == "edit_file" and record["error_type"] == "approval_denied" and record["step"] == 4

