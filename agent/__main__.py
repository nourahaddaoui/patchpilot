"""Entry point: `python -m agent` runs one remediation on sandbox/work.

    python -m agent                                   # the template repo, free rule-based model
    python -m agent --target https://github.com/o/r   # a real repo, cloned into the sandbox
    python -m agent --target https://github.com/o/r --issues github   # ...and file issues on o/r
    python -m agent --target C:/code/my-repo          # a local repo (its committed files)
    python -m agent --model gpt-4o-mini               # GPT through LangChain (paid, capped)
    python -m agent --model openai-raw:gpt-4o-mini    # GPT through the plain openai SDK (for comparison)
    python -m agent --engine loop                     # the hand-written loop (baseline) instead of LangGraph
    python -m agent --no-reset                        # run on the existing sandbox/work
    python -m agent --repo PATH                       # run on another sandbox made by sandbox/reset.sh
    python -m agent --tui                             # the terminal UI (agent/tui.py)

The real repository is never modified: the fix is committed on branch
patchpilot/fix inside sandbox/work, and pushing it is up to a human.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from dataclasses import dataclass
from typing import Callable

from agent import sandbox, targets
from agent.calllog import CallLogger
from agent.checkpoint import Checkpoint
from agent.graph import run_graph
from agent.lc_model import LangChainModel
from agent.loop import run
from agent.model import OpenAIModel, load_dotenv
from agent.rules_model import RulesModel
from agent.stop_rule import Limits, StopReason
from agent.tools.context import PROJECT_ROOT, ToolContext
from agent.tracing import enabled as tracing_enabled, run_traced

ISSUE_ARCHIVE = PROJECT_ROOT / "logs" / "issues"
ENGINES = {"loop": run, "graph": run_graph}  # same steps, same stop rule, same RunResult
DEFAULT_ENGINE = "graph"  # parity with the loop engine: tests/test_parity.py
RAW_PREFIX = "openai-raw:"


class SetupError(Exception):
    """A problem the user must fix before a run can start (bad target, no key, ...)."""


def make_model(name: str):
    """'rules' -> RulesModel (free). 'openai-raw:<name>' -> OpenAIModel (plain openai SDK).
    Anything else -> LangChainModel (ChatOpenAI; honors OPENAI_BASE_URL, e.g. Ollama).
    Raises RuntimeError / ValueError with a message for the user (no key, unknown price)."""
    if name == "rules":
        return RulesModel()
    if name.startswith(RAW_PREFIX):
        return OpenAIModel(name[len(RAW_PREFIX):])
    return LangChainModel(name)


@dataclass
class Prepared:
    model: object
    ctx: ToolContext
    limits: Limits
    logger: CallLogger
    engine: str
    target: str


def prepare(target: str | None = None, model_name: str | None = None, engine: str | None = None,
            issues: str | None = None, repo: str = str(sandbox.WORK), no_reset: bool = False,
            logger: CallLogger | None = None, say: Callable[[str], None] = print) -> Prepared:
    """Everything before the run: fetch the target, rebuild the sandbox, pick model, engine and
    issue destination. Shared by the CLI and the terminal UI. Raises SetupError."""
    load_dotenv()
    engine = engine or os.environ.get("PATCHPILOT_ENGINE") or DEFAULT_ENGINE
    if engine not in ENGINES:
        raise SetupError(f"unknown engine '{engine}': use one of {', '.join(sorted(ENGINES))}")
    try:
        model = make_model(model_name or os.environ.get("PATCHPILOT_MODEL") or "rules")
    except (RuntimeError, ValueError) as e:
        raise SetupError(str(e)) from None

    logger = logger or CallLogger()
    ctx = ToolContext(repo_root=repo, issue_archive=ISSUE_ARCHIVE)
    if issues:
        if issues not in ("local", "github"):
            raise SetupError(f"unknown issue mode '{issues}': use local or github")
        ctx.issue_mode = issues
    if ctx.github_mode:  # checked before cloning, so a bad setup fails fast
        gh = targets.github_repo(target)
        if gh:
            ctx.github_repo = gh  # issues go to the repo being fixed
        elif target:
            raise SetupError("GitHub issues need a GitHub URL as the target "
                             "(https://github.com/OWNER/REPO), or use local issues.")
        if not ctx.github_repo:
            raise SetupError("GitHub issues need --target https://github.com/OWNER/REPO "
                             "or PATCHPILOT_GITHUB_REPO in .env.")
        if not os.environ.get("GITHUB_TOKEN"):
            raise SetupError("GitHub issues need GITHUB_TOKEN in .env (a token that can write issues "
                             f"on {ctx.github_repo}).")

    if target:
        if no_reset or repo != str(sandbox.WORK):
            raise SetupError("--target always rebuilds sandbox/work; drop --no-reset and --repo.")
        try:
            say(f"Fetching {target} ...")
            source = targets.fetch(target)
            say("Preparing its test interpreter (first time only) ...")
            ctx.test_python = targets.target_python(targets.slug(target))
        except targets.TargetError as e:
            raise SetupError(str(e)) from None
        sandbox.reset(source)
        ctx.install_project = True
        ctx.install_timeout_s = 600  # real repos pin dozens of packages
        ctx.test_timeout_s = 600
    elif not no_reset and repo == str(sandbox.WORK):
        sandbox.reset()
    if not sandbox.is_sandbox(repo):
        raise SetupError(f"'{repo}' is not a sandbox. Run 'make reset' first.")
    return Prepared(model, ctx, Limits(), logger, engine, target or "sandbox/template")


def execute(p: Prepared):
    return run_traced(p.engine, ENGINES[p.engine], p.model, p.ctx, p.limits, p.logger, target=p.target)


def banner(p: Prepared) -> str:
    issues = f"GitHub issues on {p.ctx.github_repo}" if p.ctx.github_mode else "local issues"
    return (f"PatchPilot run {p.logger.run_id} on {p.target} with {p.model.name} on the {p.engine} engine, "
            f"{issues} (max {p.limits.max_steps} steps, ${p.limits.max_cost_usd:.2f}, "
            f"{p.limits.max_failed_fixes} failed fixes)")


def summary(result, p: Prepared) -> list[str]:
    lines = [f"Stop reason: {result.stop_reason.value}",
             f"Steps: {result.state.steps}   cost: ${result.state.cost_usd:.4f}   "
             f"failed fixes: {result.state.failed_fixes}"]
    if result.stop_reason is StopReason.APPROVAL_DENIED:
        lines.append("You denied the change. Nothing more was written or filed.")
    for msg in result.messages:
        if msg.get("role") != "tool" or '"filed"' not in (msg.get("content") or ""):
            continue
        try:
            info = json.loads(msg["content"])
        except json.JSONDecodeError:
            continue
        state = "filed" if info.get("filed") else "already existed"
        lines.append(f"Issue #{info.get('number')} {state}: {info.get('title')}")
        lines.append(f"  saved to: {info.get('url') or info.get('saved_to') or 'sandbox/work/.git/patchpilot-issues/'}")
    records = p.logger.read()
    if records and records[-1].get("reverted_uncommitted_edit"):
        lines.append("An untested edit was rolled back, so sandbox/work holds only tested changes.")
    if result.commit:
        lines.append(f"Committed fix {result.commit} on branch {sandbox.FIX_BRANCH} in {p.ctx.repo_root}")
        lines.append(f"  see it with: git -C {p.ctx.repo_root.as_posix()} show {result.commit}")
    lines.append(f"Trajectory: {result.log_path}")
    return lines


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m agent")
    parser.add_argument("--repo", default=str(sandbox.WORK))
    parser.add_argument("--no-reset", action="store_true", help="do not rebuild sandbox/work first")
    parser.add_argument("--target", default=None,
                        help="real repo to fix: a git URL or a local folder (default: sandbox/template)")
    parser.add_argument("--model", default=None,
                        help="'rules' (free, default), a model name for LangChain such as gpt-4o-mini or an Ollama "
                             "model with OPENAI_BASE_URL set, or openai-raw:<name> (default: $PATCHPILOT_MODEL or rules)")
    parser.add_argument("--engine", choices=sorted(ENGINES), default=None,
                        help=f"'graph' (LangGraph) or 'loop' (hand-written) (default: $PATCHPILOT_ENGINE or {DEFAULT_ENGINE})")
    parser.add_argument("--issues", choices=("local", "github"), default=None,
                        help="where create_issue files: 'local' (offline) or 'github' (on the --target GitHub repo, "
                             "needs GITHUB_TOKEN) (default: $PATCHPILOT_ISSUES or local)")
    parser.add_argument("--tui", action="store_true", help="open the terminal UI (the other flags prefill it)")
    ns = parser.parse_args(argv)

    if ns.tui:
        from agent.tui import PatchPilotApp

        PatchPilotApp(target=ns.target or "", model=ns.model or "", engine=ns.engine or "",
                      issues=ns.issues or "").run()
        return 0

    try:
        p = prepare(ns.target, ns.model, ns.engine, ns.issues, ns.repo, ns.no_reset)
    except SetupError as e:
        print(e, file=sys.stderr)
        return 2
    p.ctx.approve = Checkpoint(p.ctx, p.logger)  # human sees diff + scan before/after, then y/n
    print(banner(p))
    if tracing_enabled():
        print("LangSmith tracing is on: prompts, tool inputs/outputs and repo file contents are sent (secrets redacted).")
    result = execute(p)
    print()
    print("\n".join(summary(result, p)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
