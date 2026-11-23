"""The stop-rule proof (scripts/prove_stop_rule.py): one real run per stop reason.

check_proof() is what makes a log a proof. It is applied to fresh runs here and
to the saved logs in logs/proof/.
"""

import importlib.util
import json
import shutil
from pathlib import Path

import pytest

from agent.stop_rule import Limits, StopReason

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("prove_stop_rule", ROOT / "scripts" / "prove_stop_rule.py")
proof = importlib.util.module_from_spec(spec)
spec.loader.exec_module(proof)

WRITES = {"edit_file", "create_issue", "open_pr", "merge_pr"}
needs_git = pytest.mark.skipif(shutil.which("git") is None, reason="git not installed")


def read(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def check_proof(path: Path, limits: Limits = Limits()) -> list[str]:
    """Everything wrong with a proof log. An empty list means it proves its stop reason."""
    records = read(path)
    problems = []
    if not records or records[0].get("event") != "start":
        problems.append("does not begin with a start record")
    stops = [i for i, r in enumerate(records) if r.get("event") == "stop"]
    if len(stops) != 1 or stops[0] != len(records) - 1:
        return problems + ["must end with exactly one stop record, and nothing after it"]
    stop = records[-1]
    if stop["reason"] != path.stem:
        problems.append(f"stop reason {stop['reason']} != {path.stem}")
    tool_calls = [r for r in records if "tool" in r and "event" not in r]
    if stop["steps"] > limits.max_steps or len({r["step"] for r in tool_calls}) > limits.max_steps:
        problems.append(f"more than {limits.max_steps} steps")
    if stop.get("failed_fixes", 0) > limits.max_failed_fixes:
        problems.append("more failed fixes than the limit")
    approved, capped = None, False
    for r in records:
        if r.get("event") == "checkpoint":
            approved = r.get("approved")
        elif r.get("event") == "model" and (r.get("total_cost_usd") or 0) >= limits.max_cost_usd:
            capped = True
        elif "tool" in r and "event" not in r:
            if capped:
                problems.append(f"step {r['step']}: tool call after the cost cap")
            if r["tool"] in WRITES:
                changed = r["ok"] and (r["tool"] != "edit_file" or json.loads(r["output"]).get("changed"))
                if changed and approved is not True:
                    problems.append(f"step {r['step']}: {r['tool']} wrote without an approval")
                approved = None
    return problems


@pytest.fixture(scope="module")
def fresh(tmp_path_factory):
    out, work = tmp_path_factory.mktemp("proof"), tmp_path_factory.mktemp("work")
    return {reason: proof.prove(reason, out, work) for reason in StopReason}


@needs_git
@pytest.mark.parametrize("reason", list(StopReason), ids=lambda r: r.value)
def test_each_stop_reason_is_reached_and_its_log_is_a_proof(fresh, reason):
    r = fresh[reason]
    assert r["got"] == reason.value
    assert check_proof(Path(r["log"])) == []


@needs_git
def test_a_model_that_never_finishes_halts_at_exactly_twelve_steps(fresh):
    r = fresh[StopReason.STEP_LIMIT]
    assert r["steps"] == Limits().max_steps and r["tools"] == ["read_file"] * 12


@needs_git
def test_cost_cap_stops_before_the_tool_of_the_call_that_crossed_it(fresh):
    r = fresh[StopReason.COST_CAP]
    assert r["cost_usd"] >= Limits().max_cost_usd
    assert len(r["tools"]) == 2  # calls 1 and 2 ran their tools; call 3 crossed $0.50, its tool never ran


@needs_git
def test_three_failed_fixes_end_the_run(fresh):
    r = fresh[StopReason.FIX_ATTEMPTS]
    assert r["tools"].count("edit_file") == Limits().max_failed_fixes


@pytest.mark.parametrize("reason", list(StopReason), ids=lambda r: r.value)
def test_the_saved_proof_logs_are_valid(reason):
    path = proof.PROOF_DIR / f"{reason.value}.jsonl"
    assert path.is_file(), "run: python scripts/prove_stop_rule.py"
    assert check_proof(path) == []


def test_check_proof_catches_broken_logs(tmp_path):
    def write(name, records):
        p = tmp_path / f"{name}.jsonl"
        p.write_text("".join(json.dumps(r) + "\n" for r in records))
        return p

    start, stop = {"event": "start"}, {"event": "stop", "reason": "done", "steps": 1, "failed_fixes": 0}
    edit = {"tool": "edit_file", "step": 1, "ok": True, "output": json.dumps({"changed": True})}
    assert check_proof(write("done", [start, {"event": "checkpoint", "approved": True}, edit, stop])) == []
    assert "wrote without an approval" in check_proof(write("done", [start, edit, stop]))[0]
    assert check_proof(write("done", [start, stop, edit])) == ["must end with exactly one stop record, and nothing after it"]
    assert "!= cost_cap" in check_proof(write("cost_cap", [start, stop]))[0]
    late = [start, {"event": "model", "total_cost_usd": 0.6}, {"tool": "run_scan", "step": 1, "ok": True, "output": "{}"}, stop]
    assert "after the cost cap" in check_proof(write("done", late))[0]
