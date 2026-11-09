"""Entry point: `python -m agent` runs one remediation on sandbox/work.

    python -m agent                 # reset sandbox/work from the template, then run
    python -m agent --no-reset      # run on the existing sandbox/work
    python -m agent --repo PATH     # run on another sandbox made by sandbox/reset.sh
"""

from __future__ import annotations

import argparse
import sys

from agent import sandbox
from agent.calllog import CallLogger
from agent.checkpoint import Checkpoint
from agent.loop import run
from agent.model import OpenAIModel, load_dotenv
from agent.stop_rule import Limits, StopReason
from agent.tools.context import ToolContext


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m agent")
    parser.add_argument("--repo", default=str(sandbox.WORK))
    parser.add_argument("--no-reset", action="store_true", help="do not rebuild sandbox/work first")
    parser.add_argument("--model", default=None, help="OpenAI model (default: $PATCHPILOT_MODEL or gpt-4o-mini)")
    ns = parser.parse_args(argv)
    load_dotenv()

    if not ns.no_reset and ns.repo == str(sandbox.WORK):
        sandbox.reset()
    if not sandbox.is_sandbox(ns.repo):
        print(f"'{ns.repo}' is not a sandbox. Run 'make reset' first.", file=sys.stderr)
        return 2

    try:
        model = OpenAIModel(ns.model)
    except (RuntimeError, ValueError) as e:
        print(e, file=sys.stderr)
        return 2

    limits = Limits()
    logger = CallLogger()
    ctx = ToolContext(repo_root=ns.repo)
    ctx.approve = Checkpoint(ctx, logger)  # human sees diff + scan before/after, then y/n
    print(f"PatchPilot run {logger.run_id} on {ctx.repo_root} with {model.name} "
          f"(max {limits.max_steps} steps, ${limits.max_cost_usd:.2f}, {limits.max_failed_fixes} failed fixes)")

    result = run(model, ctx, limits, logger)

    print(f"\nStop reason: {result.stop_reason.value}")
    print(f"Steps: {result.state.steps}   cost: ${result.state.cost_usd:.4f}   failed fixes: {result.state.failed_fixes}")
    if result.stop_reason is StopReason.APPROVAL_DENIED:
        print("You denied the edit. Nothing was written; sandbox/work is unchanged.")
    if result.commit:
        print(f"Committed fix {result.commit} on branch {sandbox.FIX_BRANCH} in {ctx.repo_root}")
    print(f"Trajectory: {result.log_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
