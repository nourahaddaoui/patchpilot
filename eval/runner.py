"""Run scenarios end to end and grade them.

For each scenario and repeat: copy the template repo, apply the scenario's files,
build a real sandbox with sandbox/reset.sh, put the fake world (eval/world.py) behind
the tools, connect the real Checkpoint to a scripted human, run the real engine
(LangGraph or the loop), then grade the JSONL trajectory (eval/grade.py).

Nothing here changes how the agent behaves: the same tools, stop rule, checkpoint and
recovery run as in `python run.py`. Only trivy, pip, pytest, GitHub and the human are
stand-ins.
"""

from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
import time
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from agent import sandbox
from agent.calllog import CallLogger
from agent.checkpoint import Checkpoint
from agent.graph import run_graph
from agent.loop import run as run_loop
from agent.model import openai_tools
from agent.tools.context import PROJECT_ROOT, ToolContext
from agent.tools.registry import tool_definitions
from eval import grade
from eval.actors import ApprovingHuman, CompromisedModel, ReviewingHuman
from eval.fake_github import REPO, FakeGitHub
from eval.scenarios import Scenario
from eval.world import FakeWorld

ENGINES = {"graph": run_graph, "loop": run_loop}
TEMPLATE = PROJECT_ROOT / "sandbox" / "template"
RUNS_DIR = PROJECT_ROOT / "eval" / "runs"  # JSONL trajectories (git-ignored)


@dataclass
class Options:
    model: str = "rules"  # rules | compromised | any LLM name (see agent/__main__.make_model)
    engine: str = "graph"
    defense: bool = True  # the data-not-instructions wrapper (agent/injection.py); LLMs only
    rubber_stamp: bool = False  # injection: the human approves everything (tool guards only)


def make_model(name: str, scn: Scenario):
    if scn.model is not None:
        return scn.model()  # limits scenarios: a deliberately broken scripted model
    if name == "compromised":
        return CompromisedModel(scn.attack)
    from agent.__main__ import make_model as cli_model  # rules, or an LLM through LangChain

    return cli_model(name)


@contextmanager
def fake_github_token(on: bool):
    """GitHub-mode scenarios talk to FakeGitHub; the tools still want a token to put in a header."""
    old = os.environ.get("GITHUB_TOKEN")
    if on:
        os.environ["GITHUB_TOKEN"] = "eval-fake-token"
    try:
        yield
    finally:
        if on:
            if old is None:
                os.environ.pop("GITHUB_TOKEN", None)
            else:
                os.environ["GITHUB_TOKEN"] = old


def estimate_tokens(messages: list[dict], github_mode: bool) -> tuple[int, int]:
    """Tokens an LLM would have read and written on this exact trajectory (about 4 characters per token).
    Used for the cost model of the free rules model; LLM runs report their real usage too."""
    tools_chars = len(json.dumps(openai_tools(tool_definitions(github_mode))))
    tin = tout = 0
    for i, m in enumerate(messages):
        if m.get("role") == "assistant":
            tin += (sum(len(json.dumps(x, default=str)) for x in messages[:i]) + tools_chars) // 4
            tout += len(json.dumps({"c": m.get("content"), "t": m.get("tool_calls")}, default=str)) // 4
    return tin, tout


def build_repo(scn: Scenario, root: Path) -> Path:
    src = root / "src"
    shutil.copytree(TEMPLATE, src, ignore=shutil.ignore_patterns("__pycache__", ".pytest_cache", ".venv"))
    (src / "requirements.txt").write_text(scn.requirements, encoding="utf-8")
    for rel, text in scn.extra_files.items():
        path = src / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    return sandbox.reset(src, root / "work")


def run_scenario(scn: Scenario, opts: Options, log_dir: Path, repeat: int = 1) -> dict:
    with tempfile.TemporaryDirectory(prefix="patchpilot-eval-") as tmp:
        work = build_repo(scn, Path(tmp))
        world = FakeWorld(list(scn.advisories), fault=scn.fault, **scn.world)
        gh = FakeGitHub(scn.requirements, scn.ci_conclusion) if scn.github else None
        ctx = ToolContext(repo_root=work, test_python=sys.executable, install_deps=True, runner=world,
                          trivy_cache=Path(tmp) / "trivy-cache", issue_mode="github" if scn.github else "local",
                          github_repo=REPO if scn.github else None, http=gh, ci_poll_s=0, ci_timeout_s=5)
        logger = CallLogger(run_id=f"{scn.id}.r{repeat}", log_dir=log_dir)
        if logger.path.exists():
            logger.path.unlink()

        if scn.attack is not None and not opts.rubber_stamp:
            human = ReviewingHuman(scn.good_pin, scn.attack.red_flags)
            checkpoint = Checkpoint(ctx, logger, ask=human, show=lambda s: None)
            human.checkpoint = checkpoint
            ctx.approve = human.approve
        else:
            human = ApprovingHuman(scn.human)
            ctx.approve = Checkpoint(ctx, logger, ask=human, show=lambda s: None)

        model = make_model(opts.model, scn)
        if hasattr(model, "defense"):
            model.defense = opts.defense

        t0 = time.perf_counter()
        messages: list[dict] = []
        try:
            with fake_github_token(scn.github):
                result = ENGINES[opts.engine](model, ctx, logger=logger)
            messages = result.messages
        except Exception as e:  # a crash is a failed run, not a crashed eval
            logger.log({"event": "eval_crash", "error": f"{type(e).__name__}: {e}"[:500]})
        elapsed = time.perf_counter() - t0

        final = (work / "requirements.txt").read_text(encoding="utf-8")
        row = grade.grade_run(scn, logger.read(), final, elapsed, estimate_tokens(messages, scn.github))
        row.update({"repeat": repeat, "model": getattr(model, "name", opts.model), "engine": opts.engine,
                    "defense": opts.defense, "rubber_stamp": opts.rubber_stamp,
                    "log": logger.path.relative_to(PROJECT_ROOT).as_posix()
                    if logger.path.is_relative_to(PROJECT_ROOT) else str(logger.path)})
        return row


def run_all(scenarios: list[Scenario], opts: Options, repeats: int = 3, say=print) -> dict:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    log_dir = RUNS_DIR / f"{stamp}-{label(opts)}"
    rows = []
    for scn in scenarios:
        for k in range(1, repeats + 1):
            row = run_scenario(scn, opts, log_dir, k)
            rows.append(row)
            mark = "PASS" if row["success"] else "FAIL"
            extra = f"  [{row['blocked_by']}]" if "blocked_by" in row else ""
            say(f"{mark}  {scn.id:<38} r{k}  stop={row['stop_reason']:<16} steps={row['steps']:<3}"
                f" acc={row['step_accuracy']:.2f}{extra}")
    return report(rows, opts, scenarios, repeats, log_dir)


def label(opts: Options) -> str:
    name = opts.model.replace(":", "-").replace("/", "-")
    return f"{name}-{opts.engine}" + ("" if opts.defense else "-nodefense") + ("-rubberstamp" if opts.rubber_stamp else "")


def report(rows: list[dict], opts: Options, scenarios: list[Scenario], repeats: int, log_dir: Path) -> dict:
    cats = sorted({r["category"] for r in rows}, key=lambda c: ("fix", "failure", "limits", "human", "injection").index(c))
    return {
        "meta": {"label": label(opts), "model": opts.model, "engine": opts.engine, "defense": opts.defense,
                 "rubber_stamp": opts.rubber_stamp, "repeats": repeats, "scenarios": len(scenarios),
                 "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                 "logs": log_dir.relative_to(PROJECT_ROOT).as_posix()},
        "summary": grade.aggregate(rows),
        "dev": grade.aggregate([r for r in rows if r["split"] == "dev"]),
        "heldout": grade.aggregate([r for r in rows if r["split"] == "heldout"]),
        "by_category": {c: grade.aggregate([r for r in rows if r["category"] == c]) for c in cats},
        "runs": rows,
    }
