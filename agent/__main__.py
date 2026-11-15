"""Entry point: `python -m agent` runs one remediation on sandbox/work.

    python -m agent                                   # the template repo, free rule-based model
    python -m agent --target https://github.com/o/r   # a real repo, cloned into the sandbox
    python -m agent --target C:/code/my-repo          # a local repo (its committed files)
    python -m agent --model gpt-4o-mini               # let GPT choose the tools (paid, capped)
    python -m agent --no-reset                        # run on the existing sandbox/work
    python -m agent --repo PATH                       # run on another sandbox made by sandbox/reset.sh

The real repository is never modified: the fix is committed on branch
patchpilot/fix inside sandbox/work, and pushing it is up to a human.
"""

from __future__ import annotations

import argparse
import json
import os
import sys

from agent import sandbox, targets
from agent.calllog import CallLogger
from agent.checkpoint import Checkpoint
from agent.loop import run
from agent.model import OpenAIModel, load_dotenv
from agent.rules_model import RulesModel
from agent.stop_rule import Limits, StopReason
from agent.tools.context import PROJECT_ROOT, ToolContext

ISSUE_ARCHIVE = PROJECT_ROOT / "logs" / "issues"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m agent")
    parser.add_argument("--repo", default=str(sandbox.WORK))
    parser.add_argument("--no-reset", action="store_true", help="do not rebuild sandbox/work first")
    parser.add_argument("--target", default=None,
                        help="real repo to fix: a git URL or a local folder (default: sandbox/template)")
    parser.add_argument("--model", default=None,
                        help="'rules' (free, default) or an OpenAI model such as gpt-4o-mini (default: $PATCHPILOT_MODEL or rules)")
    ns = parser.parse_args(argv)
    load_dotenv()

    test_python = None
    if ns.target:
        if ns.no_reset or ns.repo != str(sandbox.WORK):
            print("--target always rebuilds sandbox/work; drop --no-reset and --repo.", file=sys.stderr)
            return 2
        try:
            print(f"Fetching {ns.target} ...")
            source = targets.fetch(ns.target)
            print("Preparing its test interpreter (first time only) ...")
            test_python = targets.target_python(targets.slug(ns.target))
        except targets.TargetError as e:
            print(e, file=sys.stderr)
            return 2
        sandbox.reset(source)
    elif not ns.no_reset and ns.repo == str(sandbox.WORK):
        sandbox.reset()
    if not sandbox.is_sandbox(ns.repo):
        print(f"'{ns.repo}' is not a sandbox. Run 'make reset' first.", file=sys.stderr)
        return 2

    name = ns.model or os.environ.get("PATCHPILOT_MODEL") or "rules"
    if name == "rules":
        model = RulesModel()
    else:
        try:
            model = OpenAIModel(name)
        except (RuntimeError, ValueError) as e:
            print(e, file=sys.stderr)
            return 2

    limits = Limits()
    logger = CallLogger()
    ctx = ToolContext(repo_root=ns.repo, issue_archive=ISSUE_ARCHIVE)
    if test_python:
        ctx.test_python = test_python
        ctx.install_project = True
    ctx.approve = Checkpoint(ctx, logger)  # human sees diff + scan before/after, then y/n
    print(f"PatchPilot run {logger.run_id} on {ctx.repo_root} with {model.name} "
          f"(max {limits.max_steps} steps, ${limits.max_cost_usd:.2f}, {limits.max_failed_fixes} failed fixes)")

    result = run(model, ctx, limits, logger)

    print(f"\nStop reason: {result.stop_reason.value}")
    print(f"Steps: {result.state.steps}   cost: ${result.state.cost_usd:.4f}   failed fixes: {result.state.failed_fixes}")
    if result.stop_reason is StopReason.APPROVAL_DENIED:
        print("You denied the change. Nothing was written or filed.")
    for msg in result.messages:
        if msg.get("role") != "tool" or '"filed"' not in (msg.get("content") or ""):
            continue
        try:
            info = json.loads(msg["content"])
        except json.JSONDecodeError:
            continue
        state = "filed" if info.get("filed") else "already existed"
        print(f"Issue #{info.get('number')} {state}: {info.get('title')}")
        print(f"  saved to: {info.get('url') or info.get('saved_to') or 'sandbox/work/.git/patchpilot-issues/'}")
    if logger.read()[-1].get("reverted_uncommitted_edit"):
        print("An untested edit was rolled back, so sandbox/work holds only tested changes.")
    if result.commit:
        print(f"Committed fix {result.commit} on branch {sandbox.FIX_BRANCH} in {ctx.repo_root}")
        print(f"  see it with: git -C {ctx.repo_root.as_posix()} show {result.commit}")
    print(f"Trajectory: {result.log_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
