"""Recovery: retries, rollback, and cleanup after partial failure (Day 6).

1. Retry transient tool failures once (scanner timeout, garbled output, internal
   error) before the model ever sees them. Write tools are never retried
   automatically: a retry could ask the human twice or write twice.
2. Roll back a bad edit: when the tests go red (or the install fails) right after
   an edit, requirements.txt is restored to the last version the tests passed on
   (or the starting file), so the next attempt starts from a known-good file. The
   model is told in the tool result.
3. Clean up after partial failure: a run that ends without DONE never leaves an
   untested edit behind; it goes back to the last tested-good version. Edits the
   tests already passed on stay (a human approved them and they work).

Every recovery action is logged as an event "recovery".
"""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

from agent import sandbox
from agent.calllog import CallLogger
from agent.tools.context import ToolContext
from agent.tools.registry import TOOLS, ToolResult, call_tool

RETRYABLE = {"timeout", "bad_output", "internal"}
MAX_RETRIES = 1
EDITABLE = "requirements.txt"


def call_with_retry(name: str, args, ctx: ToolContext, logger: CallLogger | None, step: int,
                    max_retries: int = MAX_RETRIES) -> ToolResult:
    result = call_tool(name, args, ctx, logger, step=step)
    tool = TOOLS.get(name)
    retries = 0
    while (not result.ok and result.error_type in RETRYABLE and retries < max_retries
           and tool is not None and not tool.writes):
        retries += 1
        if logger is not None:
            logger.log({"event": "recovery", "action": "retry", "step": step, "tool": name,
                        "error_type": result.error_type, "attempt": retries + 1})
        result = call_tool(name, args, ctx, logger, step=step)
    return result


def snapshot(repo: Path | str) -> str | None:
    """The current requirements.txt text (the known-good version to return to)."""
    path = Path(repo) / EDITABLE
    return path.read_text(encoding="utf-8") if path.is_file() else None


def rollback(repo: Path | str, good: str | None) -> str | None:
    """Put requirements.txt back to `good`. Returns the restored text, or None if
    nothing changed. Sandbox only."""
    repo = Path(repo)
    if good is None or not sandbox.is_sandbox(repo) or snapshot(repo) == good:
        return None
    (repo / EDITABLE).write_text(good, encoding="utf-8")
    return good


def rollback_after_failed_tests(ctx: ToolContext, result: ToolResult, good: str | None,
                                logger: CallLogger | None, step: int) -> ToolResult:
    """Called when tests went red right after an edit. Rolls back and says so in the result."""
    restored = rollback(ctx.repo_root, good)
    if restored is None:
        return result
    if logger is not None:
        logger.log({"event": "recovery", "action": "rollback", "step": step,
                    "reason": result.error_type or "tests_red", "restored": restored[:500]})
    note = ("\n[recovery] The tests failed after your edit, so requirements.txt was rolled back "
            "to its last good version. Try another version, or call create_issue so a human can decide.")
    return replace(result, content=result.content + note)


def cleanup_after_run(ctx: ToolContext, done: bool, good: str | None, logger: CallLogger | None) -> bool:
    """After a run that did not end DONE, go back to the last tested-good file. True if it reverted."""
    if done:
        return False
    restored = rollback(ctx.repo_root, good)
    if restored is None:
        return False
    if logger is not None:
        logger.log({"event": "recovery", "action": "revert_partial", "restored": restored[:500]})
    return True
