"""One test per error class in errors.md: what the agent does when something goes wrong.

Every test runs the real loop on a fresh sandbox of the template, with the real
tools, checkpoint, recovery and stop rule. Only trivy and pip/pytest are
replaced (Fake below), and Fake can make them fail on purpose.
"""

import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from agent import sandbox
from agent.calllog import CallLogger
from agent.checkpoint import Checkpoint, ScriptedHuman
from agent.loop import run
from agent.model import ModelReply, ScriptedModel
from agent.rules_model import RulesModel
from agent.stop_rule import StopReason
from agent.tools.context import ToolContext
from tests.conftest import TEMPLATE

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="git not installed")

FIX = [("run_scan", {}), ("read_file", {"path": "requirements.txt"}),
       ("edit_file", {"path": "requirements.txt", "package": "requests", "version": "2.31.0"}),
       ("run_tests", {}), ("run_scan", {})]


class Fake:
    """trivy and pip/pytest stand-ins. `faults` maps a program to a list of failures,
    one used per call: "timeout", "missing" (not installed), "empty" (no output)."""

    def __init__(self, vulns=None, red_after_bump=False, missing_versions=(), faults=None):
        self.vulns = vulns or {"requests": ("2.25.1", "2.31.0")}  # package -> (vulnerable pin, fixed)
        self.red_after_bump = red_after_bump
        self.missing_versions = set(missing_versions)
        self.faults = {k: list(v) for k, v in (faults or {}).items()}

    @staticmethod
    def pins(folder):
        req = folder / "requirements.txt"
        lines = req.read_text().splitlines() if req.is_file() else []
        return dict(line.split("==", 1) for line in lines if "==" in line)

    def _fault(self, program, cmd, kwargs):
        if not self.faults.get(program):
            return None
        kind = self.faults[program].pop(0)
        if kind == "timeout":
            raise subprocess.TimeoutExpired(cmd, kwargs.get("timeout") or 0)
        if kind == "missing":
            raise FileNotFoundError(cmd[0])
        return subprocess.CompletedProcess(cmd, 0, "", "")  # "empty"

    def __call__(self, cmd, cwd=None, **kwargs):
        cmd = [str(c) for c in cmd]
        if cmd[0] == "trivy":
            hit = self._fault("trivy", cmd, kwargs)
            if hit:
                return hit
            pins = self.pins(Path(cmd[-1]))
            vulns = [{"VulnerabilityID": f"CVE-{pkg}", "PkgName": pkg, "InstalledVersion": bad, "FixedVersion": fixed,
                      "Severity": "MEDIUM", "Title": f"{pkg} issue"}
                     for pkg, (bad, fixed) in self.vulns.items() if pins.get(pkg) == bad]
            return subprocess.CompletedProcess(cmd, 0, json.dumps({"Results": [{"Target": "requirements.txt",
                                                                                 "Vulnerabilities": vulns}]}), "")
        if cmd[1:3] == ["-m", "pip"]:
            hit = self._fault("pip", cmd, kwargs)
            if hit:
                return hit
            pins = self.pins(Path(cwd))
            bad = [f"{p}=={v}" for p, v in pins.items() if f"{p}=={v}" in self.missing_versions]
            if bad:
                return subprocess.CompletedProcess(cmd, 1, "", f"ERROR: No matching distribution found for {bad[0]}")
            return subprocess.CompletedProcess(cmd, 0, "", "")
        if cmd[1:3] == ["-m", "pytest"]:
            hit = self._fault("pytest", cmd, kwargs)
            if hit:
                return hit
            pins = self.pins(Path(cwd))
            red = self.red_after_bump and any(pins.get(p) != bad for p, (bad, _) in self.vulns.items() if p in pins)
            out = "..F\n1 failed, 2 passed in 0.1s\n" if red else "...\n3 passed in 0.1s\n"
            return subprocess.CompletedProcess(cmd, 1 if red else 0, out, "")
        return subprocess.CompletedProcess(cmd, 5, "", "")


def go(tmp_path, model, fake=None, answers=("y", "y", "y"), requirements=None):
    work = sandbox.reset(TEMPLATE, tmp_path / "work")
    if requirements is not None:  # a variant of the template, committed as the sandbox baseline
        (work / "requirements.txt").write_text(requirements)
        subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qam", "variant"], cwd=work)
    logger = CallLogger(run_id="e", log_dir=tmp_path / "logs")
    ctx = ToolContext(repo_root=work, test_python=sys.executable, runner=fake or Fake(),
                      issue_mode="local", github_repo=None)
    ctx.approve = Checkpoint(ctx, logger, ask=ScriptedHuman(list(answers)), show=lambda t: None)
    result = run(model, ctx, logger=logger)
    records = logger.read()
    calls = [r for r in records if "tool" in r and "event" not in r]
    return result, calls, records, (work / "requirements.txt").read_text()


# --- tool down ------------------------------------------------------------------------

def test_tool_down_is_not_retried_and_the_run_is_handed_off(tmp_path):
    result, calls, records, req = go(tmp_path, RulesModel(), Fake(faults={"trivy": ["missing"] * 5}))
    assert calls[0]["error_type"] == "tool_down" and "Traceback" not in calls[0]["output"]
    assert not [r for r in records if r.get("event") == "recovery"]  # a missing program is not retried
    assert result.stop_reason is StopReason.HANDED_OFF
    assert [c["tool"] for c in calls] == ["run_scan", "create_issue"] and req == "requests==2.25.1\n"


# --- bad arguments --------------------------------------------------------------------

def test_bad_arguments_are_rejected_before_the_tool_runs(tmp_path):
    model = ScriptedModel([("read_file", {"pathh": "requirements.txt"})] + FIX)
    result, calls, _, req = go(tmp_path, model)
    assert calls[0]["error_type"] == "bad_arguments"
    assert "'path' is required" in calls[0]["output"] and "unexpected argument 'pathh'" in calls[0]["output"]
    assert result.stop_reason is StopReason.DONE and result.state.steps == 6  # the bad call still cost a step
    assert req == "requests==2.31.0\n"


def test_bad_arguments_to_the_write_tool_never_reach_the_human(tmp_path):
    bad_edit = ("edit_file", {"path": "requirements.txt", "package": "requests", "version": "2.31.0; rm -rf /"})
    model = ScriptedModel([("run_scan", {}), bad_edit, ModelReply(text="stop"), ModelReply(text="stop")])
    result, calls, records, req = go(tmp_path, model)
    assert calls[1]["error_type"] == "bad_arguments"
    assert not [r for r in records if r.get("event") == "checkpoint"] and req == "requests==2.25.1\n"


# --- empty result ---------------------------------------------------------------------

def test_empty_scan_output_is_retried_once_not_taken_as_clean(tmp_path):
    result, calls, records, req = go(tmp_path, RulesModel(), Fake(faults={"trivy": ["empty"]}))
    assert calls[0]["error_type"] == "bad_output" and calls[1]["tool"] == "run_scan" and calls[1]["ok"]
    assert [r["action"] for r in records if r.get("event") == "recovery"] == ["retry"]
    assert result.stop_reason is StopReason.DONE and req == "requests==2.31.0\n"


# --- wrong tool -----------------------------------------------------------------------

def test_a_tool_that_does_not_exist_gets_the_list_of_real_tools(tmp_path):
    model = ScriptedModel([("run_scan", {}), ("delete_file", {"path": "tests/test_client.py"})] + FIX[1:])
    result, calls, _, req = go(tmp_path, model)
    assert calls[1]["error_type"] == "unknown_tool"
    assert "Available tools: run_scan, read_file" in calls[1]["output"]
    assert result.stop_reason is StopReason.DONE and req == "requests==2.31.0\n"
    assert (result.state.steps, [c["tool"] for c in calls if c["ok"]].count("edit_file")) == (6, 1)


# --- loop -----------------------------------------------------------------------------

def test_a_model_stuck_in_a_loop_stops_at_twelve_steps(tmp_path):
    result, calls, _, _ = go(tmp_path, ScriptedModel([("run_scan", {})]))
    assert result.stop_reason is StopReason.STEP_LIMIT and len(calls) == 12


def test_repeating_the_same_edit_changes_nothing_and_asks_nobody(tmp_path):
    edit = FIX[2]
    model = ScriptedModel([FIX[0], FIX[1], edit, edit, ("run_tests", {}), ("run_scan", {})])
    result, calls, records, req = go(tmp_path, model)
    assert json.loads(calls[3]["output"])["changed"] is False
    assert len([r for r in records if r.get("event") == "checkpoint"]) == 1
    assert result.stop_reason is StopReason.DONE and req == "requests==2.31.0\n"


def test_a_model_that_stops_calling_tools_is_nudged_once_then_stopped(tmp_path):
    result, calls, _, _ = go(tmp_path, ScriptedModel([ModelReply(text="All good, I think.")]))
    assert result.stop_reason is StopReason.GAVE_UP and calls == [] and result.state.steps == 2


# --- timeout --------------------------------------------------------------------------

def test_scan_timeout_once_is_retried_and_the_fix_goes_through(tmp_path):
    result, calls, records, req = go(tmp_path, RulesModel(), Fake(faults={"trivy": ["timeout"]}))
    assert calls[0]["error_type"] == "timeout" and calls[1]["ok"]
    assert result.stop_reason is StopReason.DONE and req == "requests==2.31.0\n"


def test_tests_timing_out_after_an_edit_roll_the_edit_back(tmp_path):
    result, calls, records, req = go(tmp_path, RulesModel(), Fake(faults={"pip": ["timeout", "timeout"]}))
    tests = [c for c in calls if c["tool"] == "run_tests"]
    assert [c["error_type"] for c in tests] == ["timeout", "timeout"]  # retried once
    assert any(r.get("action") == "rollback" for r in records if r.get("event") == "recovery")
    assert result.stop_reason is StopReason.HANDED_OFF and req == "requests==2.25.1\n"


# --- tests red / version does not exist --------------------------------------------------

def test_a_bump_that_breaks_the_tests_is_rolled_back_and_handed_off(tmp_path):
    result, calls, _, req = go(tmp_path, RulesModel(), Fake(red_after_bump=True))
    assert [c["tool"] for c in calls] == ["run_scan", "read_file", "edit_file", "run_tests", "create_issue"]
    assert result.stop_reason is StopReason.HANDED_OFF and req == "requests==2.25.1\n"


def test_a_version_pip_cannot_install_counts_as_a_failed_fix(tmp_path):
    result, calls, _, req = go(tmp_path, RulesModel(), Fake(missing_versions={"requests==2.31.0"}))
    tests = [c for c in calls if c["tool"] == "run_tests"][0]
    assert tests["error_type"] == "install_failed" and result.state.failed_fixes == 1
    assert result.stop_reason is StopReason.HANDED_OFF and req == "requests==2.25.1\n"


# --- user changes mind ----------------------------------------------------------------

def test_user_approves_one_edit_then_denies_the_next(tmp_path):
    fake = Fake(vulns={"requests": ("2.25.1", "2.31.0"), "urllib3": ("1.26.5", "1.26.19")})
    result, calls, records, req = go(tmp_path, RulesModel(), fake, answers=["y", "n"],
                                     requirements="requests==2.25.1\nurllib3==1.26.5\n")
    decisions = [r["approved"] for r in records if r.get("event") == "checkpoint"]
    assert decisions == [True, False]
    assert result.stop_reason is StopReason.APPROVAL_DENIED
    assert req == "requests==2.31.0\nurllib3==1.26.5\n"  # the approved, tested edit stays; nothing else


def test_no_answer_at_all_is_a_no(tmp_path):
    result, _, records, req = go(tmp_path, RulesModel(), answers=[])
    assert [r["answer"] for r in records if r.get("event") == "checkpoint"] == ["<no input>"]
    assert result.stop_reason is StopReason.APPROVAL_DENIED and req == "requests==2.25.1\n"
