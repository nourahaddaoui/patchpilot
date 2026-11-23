"""Prove the stop rule: run the real agent once per stop reason and keep each log.

    python scripts/prove_stop_rule.py            # writes logs/proof/<reason>.jsonl
    python scripts/prove_stop_rule.py --out DIR

Every run is the real loop (agent/loop.py) on a fresh sandbox clone of the
template, with the real tools, the real human checkpoint (a scripted human
answers it), recovery and the JSONL log. Only the two external programs are
stand-ins, so the proof runs offline and gives the same result every time:
trivy reports the requests CVE while the pin is below the fix, and pytest goes
green or red as each scenario says.

One run per stop reason (agent/stop_rule.py):

    done              rules model fixes the CVE: scan, read, edit, test, rescan
    approval_denied   the human says no to the edit
    no_fixed_version  the CVE has no fixed version: one issue, then stop
    handed_off        the bump breaks the tests: rolled back, one issue, stop
    fix_attempts      a model that keeps bumping to versions that break the tests
    cost_cap          a model whose every call costs $0.20 (cap $0.50)
    step_limit        a model that never finishes (reads the same file forever)
    gave_up           a model that never calls a tool (nudged once, then stopped)
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from agent import sandbox  # noqa: E402
from agent.calllog import CallLogger  # noqa: E402
from agent.checkpoint import Checkpoint, ScriptedHuman  # noqa: E402
from agent.loop import run  # noqa: E402
from agent.model import ModelReply, ScriptedModel  # noqa: E402
from agent.rules_model import RulesModel  # noqa: E402
from agent.stop_rule import Limits, StopReason  # noqa: E402
from agent.tools.context import ToolContext  # noqa: E402

PROOF_DIR = ROOT / "logs" / "proof"
TEMPLATE = ROOT / "sandbox" / "template"
BASELINE = "2.25.1"
CVE = "CVE-2023-32681"


class StandIn:
    """Replaces subprocess.run for trivy and pip/pytest only (ToolContext.runner)."""

    def __init__(self, fixed: str | None = "2.31.0", tests_break_after_bump: bool = False):
        self.fixed = fixed
        self.tests_break_after_bump = tests_break_after_bump

    def _pin(self, folder: Path) -> str | None:
        req = folder / "requirements.txt"
        for line in (req.read_text(encoding="utf-8") if req.is_file() else "").splitlines():
            name, _, version = line.partition("==")
            if name.strip().lower() == "requests":
                return version.split()[0] if version.split() else None
        return None

    def __call__(self, cmd, cwd=None, **kwargs):
        cmd = [str(c) for c in cmd]
        if cmd[0] == "trivy":
            vulns = []
            if self._pin(Path(cmd[-1])) == BASELINE:
                vulns = [{"VulnerabilityID": CVE, "PkgName": "requests", "InstalledVersion": BASELINE,
                          "FixedVersion": self.fixed or "", "Severity": "MEDIUM",
                          "Title": "requests: Unintended leak of Proxy-Authorization header"}]
            report = {"Results": [{"Target": "requirements.txt", "Vulnerabilities": vulns}]}
            return subprocess.CompletedProcess(cmd, 0, json.dumps(report), "")
        if cmd[1:3] == ["-m", "pip"]:
            return subprocess.CompletedProcess(cmd, 0, "", "")
        if cmd[1:3] == ["-m", "pytest"]:
            red = self.tests_break_after_bump and self._pin(Path(cwd)) != BASELINE
            out = "..F\nE   AttributeError: removed API\n1 failed, 2 passed in 0.10s\n" if red else "...\n3 passed in 0.10s\n"
            return subprocess.CompletedProcess(cmd, 1 if red else 0, out, "")
        return subprocess.CompletedProcess(cmd, 5, "", "")


FIX = [("run_scan", {}), ("read_file", {"path": "requirements.txt"}),
       ("edit_file", {"path": "requirements.txt", "package": "requests", "version": "2.31.0"}),
       ("run_tests", {}), ("run_scan", {})]
KEEPS_BREAKING = [("run_scan", {}), ("read_file", {"path": "requirements.txt"})] + [
    step for v in ("2.31.0", "2.32.0", "2.32.4")
    for step in (("edit_file", {"path": "requirements.txt", "package": "requests", "version": v}), ("run_tests", {}))
]

# reason: (model factory, stand-in, human answers)
PROOFS = {
    StopReason.DONE: (RulesModel, StandIn(), ["y"]),
    StopReason.APPROVAL_DENIED: (RulesModel, StandIn(), ["n"]),
    StopReason.NO_FIXED_VERSION: (RulesModel, StandIn(fixed=None), ["y"]),
    StopReason.HANDED_OFF: (RulesModel, StandIn(tests_break_after_bump=True), ["y", "y"]),
    StopReason.FIX_ATTEMPTS: (lambda: ScriptedModel(KEEPS_BREAKING), StandIn(tests_break_after_bump=True), ["y"] * 3),
    StopReason.COST_CAP: (lambda: ScriptedModel(FIX, cost_per_call=0.20), StandIn(), ["y"]),
    StopReason.STEP_LIMIT: (lambda: ScriptedModel([("read_file", {"path": "requirements.txt"})]), StandIn(), []),
    StopReason.GAVE_UP: (lambda: ScriptedModel([ModelReply(text="I think we are done.")]), StandIn(), []),
}


def prove(reason: StopReason, out_dir: Path, work_root: Path, limits: Limits = Limits()) -> dict:
    make_model, stand_in, answers = PROOFS[reason]
    log = out_dir / f"{reason.value}.jsonl"
    log.unlink(missing_ok=True)
    work = sandbox.reset(TEMPLATE, work_root / reason.value)
    logger = CallLogger(run_id=reason.value, log_dir=out_dir)
    ctx = ToolContext(repo_root=work, test_python=sys.executable, runner=stand_in,
                      issue_mode="local", github_repo=None)
    ctx.approve = Checkpoint(ctx, logger, ask=ScriptedHuman(answers), show=lambda text: None)
    result = run(make_model(), ctx, limits=limits, logger=logger)
    tools = [r["tool"] for r in logger.read() if "tool" in r and "event" not in r]
    return {"expected": reason.value, "got": result.stop_reason.value, "steps": result.state.steps,
            "cost_usd": round(result.state.cost_usd, 2), "tools": tools, "log": str(log)}


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Run the agent once per stop reason and keep each log.")
    p.add_argument("--out", default=str(PROOF_DIR))
    a = p.parse_args(argv)
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    ok = True
    with tempfile.TemporaryDirectory(prefix="patchpilot-proof-") as tmp:
        for reason in StopReason:
            r = prove(reason, out, Path(tmp))
            good = r["got"] == r["expected"]
            ok &= good
            print(f"{'OK  ' if good else 'FAIL'} {r['expected']:17} -> {r['got']:17} {r['steps']:>2} steps  "
                  f"${r['cost_usd']:.2f}  {' '.join(r['tools'])}")
    print(f"\nlogs: {out}")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
