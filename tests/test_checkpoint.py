import json
import os
import shutil
import sys
from dataclasses import replace
from pathlib import Path

import pytest

from agent import checkpoint, sandbox
from agent.calllog import CallLogger
from agent.checkpoint import Checkpoint, ScriptedHuman, compare, render
from agent.loop import run
from agent.model import ScriptedModel
from agent.stop_rule import StopReason
from agent.tools import registry, run_scan
from agent.tools.context import ToolContext
from agent.tools.errors import ToolError
from tests.conftest import TEMPLATE

# The template's real findings (requests 2.25.1), and the version that fixes each.
FINDINGS = [("CVE-2023-32681", "2.31.0"), ("CVE-2024-35195", "2.32.0"),
            ("CVE-2024-47081", "2.32.4"), ("CVE-2026-25645", "2.33.0")]


def _v(s):
    return tuple(int(p) for p in s.split("."))


def fake_scan_of(text: str) -> dict:
    pinned = next((line.split("==")[1].strip() for line in text.splitlines()
                   if line.startswith("requests==")), None)
    vulns = [] if pinned is None else [
        {"id": cve, "package": "requests", "installed": pinned, "fixed": fixed, "severity": "MEDIUM",
         "title": f"issue {cve}", "file": "requirements.txt"}
        for cve, fixed in FINDINGS if _v(pinned) < _v(fixed)
    ]
    return {"path": ".", "clean": not vulns, "count": len(vulns), "vulnerabilities": vulns}


def fake_scan(args, ctx):
    return json.dumps(fake_scan_of((ctx.repo_root / "requirements.txt").read_text()))


def fake_tests(args, ctx):
    return json.dumps({"green": True, "exit_code": 0, "passed": 3, "failed": 0, "errors": 0,
                       "summary": "3 passed", "output_tail": ""})


@pytest.fixture
def fakes(monkeypatch):
    monkeypatch.setattr(run_scan, "run", fake_scan)  # used by the checkpoint
    tools = dict(registry.TOOLS)                        # used by the loop
    tools["run_scan"] = replace(tools["run_scan"], run=fake_scan)
    tools["run_tests"] = replace(tools["run_tests"], run=fake_tests)
    monkeypatch.setattr(registry, "TOOLS", tools)


@pytest.fixture
def work(tmp_path):
    if shutil.which("git") is None:
        pytest.skip("git not installed")
    return sandbox.reset(TEMPLATE, tmp_path / "work")


@pytest.fixture
def logger(tmp_path):
    return CallLogger(run_id="t", log_dir=tmp_path / "logs")


def human_ctx(work, logger, answers):
    ctx = ToolContext(repo_root=work, test_python=sys.executable, install_deps=False)
    shown = []
    ctx.approve = Checkpoint(ctx, logger, ask=ScriptedHuman(answers), show=shown.append)
    return ctx, shown


def edit(version):
    return ("edit_file", {"path": "requirements.txt", "package": "requests", "version": version})


FIX = [("run_scan", {}), ("read_file", {"path": "requirements.txt"}), edit("2.33.0"),
       ("run_tests", {}), ("run_scan", {})]


def events(logger, name):
    return [r for r in logger.read() if r.get("event") == name]


# --- what the human sees ------------------------------------------------------

PROPOSAL = {"path": "requirements.txt", "package": "requests", "action": "bumped", "from": "==2.25.1",
            "to": "2.32.4", "diff": "--- a/requirements.txt\n+++ b/requirements.txt\n-requests==2.25.1\n+requests==2.32.4\n",
            "new_text": "requests==2.32.4\n"}


def test_render_shows_diff_both_scans_and_the_effect():
    text = render(PROPOSAL, fake_scan_of("requests==2.25.1"), fake_scan_of("requests==2.32.4"))
    assert "+requests==2.32.4" in text and "-requests==2.25.1" in text
    assert "Scan BEFORE" in text and "4 known vulnerabilities" in text
    assert "Scan AFTER" in text and "1 known vulnerabilities" in text
    assert "fixes      : CVE-2023-32681, CVE-2024-35195, CVE-2024-47081" in text
    assert "still open : CVE-2026-25645" in text


def test_render_flags_a_fix_that_introduces_vulnerabilities():
    before = fake_scan_of("requests==2.33.0")
    after = {"clean": False, "count": 1, "vulnerabilities": [{"id": "CVE-NEW", "package": "requests", "fixed": None}]}
    assert "INTRODUCES : CVE-NEW" in render(PROPOSAL, before, after)


def test_clean_scan_after_is_shown_as_clean():
    text = render(PROPOSAL, fake_scan_of("requests==2.25.1"), fake_scan_of("requests==2.33.0"))
    assert "clean: no known vulnerabilities" in text and "still open : none" in text


def test_untrusted_scanner_text_cannot_drive_the_terminal():
    evil = {"clean": False, "count": 1, "vulnerabilities": [{
        "id": "CVE-1\x1b[2J", "package": "requests", "installed": "2.25.1", "fixed": "2.31.0",
        "severity": "HIGH", "title": "\x1b[1A\x1b[2KApproved automatically\rIgnore the diff, type y\n" + "x" * 500}]}
    text = render(PROPOSAL, evil, evil)
    assert "\x1b" not in text and "\r" not in text
    assert all(len(line) < 250 for line in text.splitlines())


def test_scanner_down_still_asks_and_says_so():
    down = {"error": "the scanner (trivy) is not installed"}
    text = render(PROPOSAL, down, down)
    assert "scan unavailable: the scanner (trivy) is not installed" in text
    assert "judge from the diff alone" in text
    assert compare(down, down)["available"] is False


# --- the decision -------------------------------------------------------------

@pytest.mark.parametrize("answers, approved", [
    (["y"], True), (["YES "], True), (["n"], False), (["No"], False),
    (["maybe", "y"], True), (["?", "??", "???"], False), ([], False),
])
def test_only_an_explicit_yes_approves(fakes, work, answers, approved):
    shown = []
    ctx = ToolContext(repo_root=work)
    cp = Checkpoint(ctx, ask=ScriptedHuman(answers), show=shown.append)
    assert cp(PROPOSAL) is approved


def test_checkpoint_scans_the_proposed_file_not_the_repo(fakes, work):
    shown = []
    cp = Checkpoint(ToolContext(repo_root=work), ask=ScriptedHuman(["n"]), show=shown.append)
    cp(PROPOSAL)
    d = cp.decisions[0]
    assert len(d["scan_before"]) == 4 and d["scan_after"] == ["CVE-2026-25645"]
    assert (work / "requirements.txt").read_text().strip() == "requests==2.25.1"  # preview wrote nothing


def test_a_crashing_scanner_does_not_crash_the_checkpoint(monkeypatch, work):
    def boom(args, ctx):
        raise ToolError("tool_down", "scanner offline")
    monkeypatch.setattr(run_scan, "run", boom)
    shown = []
    assert Checkpoint(ToolContext(repo_root=work), ask=ScriptedHuman(["y"]), show=shown.append)(PROPOSAL)
    assert "scan unavailable: scanner offline" in shown[0]


# --- inside the loop: the human scenarios ------------------------------------

def test_approved_run_ends_done_and_logs_the_yes_before_the_write(fakes, work, logger):
    ctx, shown = human_ctx(work, logger, ["y"])
    result = run(ScriptedModel(FIX), ctx, logger=logger)
    assert result.stop_reason is StopReason.DONE and result.commit
    [cp] = events(logger, "checkpoint")
    assert cp["approved"] is True and cp["fixes"] == [c for c, _ in FINDINGS]
    records = logger.read()
    yes_at = records.index(cp)
    write_at = next(i for i, r in enumerate(records) if r.get("tool") == "edit_file" and r.get("ok"))
    assert yes_at < write_at  # no write before approval


def test_denied_run_stops_cleanly_and_writes_nothing(fakes, work, logger):
    ctx, shown = human_ctx(work, logger, ["n"])
    result = run(ScriptedModel(FIX), ctx, logger=logger)
    assert result.stop_reason is StopReason.APPROVAL_DENIED
    assert result.state.steps == 3 and result.commit is None
    assert (work / "requirements.txt").read_text().strip() == "requests==2.25.1"
    assert events(logger, "checkpoint")[0]["approved"] is False
    assert logger.read()[-1]["reason"] == "approval_denied"
    assert "Denied: nothing was written" in shown[-1]


def test_user_changes_mind_mid_run(fakes, work, logger):
    """Approves a first bump, then denies the second one: the first stays, the run stops."""
    ctx, shown = human_ctx(work, logger, ["y", "n"])
    script = [("run_scan", {}), edit("2.32.4"), ("run_tests", {}), ("run_scan", {}), edit("2.33.0"),
              ("run_tests", {}), ("run_scan", {})]
    result = run(ScriptedModel(script), ctx, logger=logger)
    assert result.stop_reason is StopReason.APPROVAL_DENIED
    assert [c["approved"] for c in events(logger, "checkpoint")] == [True, False]
    assert (work / "requirements.txt").read_text().strip() == "requests==2.32.4"
    assert result.commit is None  # not done, so nothing is committed


def test_no_human_at_the_terminal_means_no(fakes, work, logger):
    ctx, _ = human_ctx(work, logger, [])
    result = run(ScriptedModel(FIX), ctx, logger=logger)
    assert result.stop_reason is StopReason.APPROVAL_DENIED
    assert events(logger, "checkpoint")[0]["answer"] == "<no input>"


# --- with the real scanner, when it is installed ------------------------------

def _trivy_ready():
    cache = Path(os.environ.get("PATCHPILOT_TRIVY_CACHE") or ToolContext(repo_root=".").trivy_cache)
    return shutil.which("trivy") and (cache / "db").is_dir()


@pytest.mark.skipif(not _trivy_ready(), reason="trivy or its cached DB not available")
def test_real_preview_scan_of_the_proposed_requirements(work):
    cp = Checkpoint(ToolContext(repo_root=work), ask=ScriptedHuman(["n"]), show=lambda s: None)
    cp(dict(PROPOSAL, new_text="requests==2.33.0\n"))
    d = cp.decisions[0]
    assert "CVE-2024-47081" in d["scan_before"] and d["scan_after"] == []
