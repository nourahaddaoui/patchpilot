"""Tool registry and the single entry point for every tool call.

call_tool() validates arguments against the tool's strict schema, runs the tool,
turns every failure into a plain-text error (never a stack trace), and logs the
call (input, output, ok, error type, duration) to the run's JSONL log.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import datetime, timezone
from types import ModuleType
from typing import Callable

from agent.calllog import MAX_LOGGED_OUTPUT, CallLogger
from agent.tools import edit_file, read_file, run_scan, run_tests
from agent.tools.context import ToolContext
from agent.tools.errors import ToolError
from agent.tools.schema import validate


@dataclass(frozen=True)
class Tool:
    name: str
    description: str
    input_schema: dict
    run: Callable[[dict, ToolContext], str]
    writes: bool = False


def _from_module(module: ModuleType, writes: bool = False) -> Tool:
    return Tool(module.NAME, module.DESCRIPTION, module.INPUT_SCHEMA, module.run, writes)


TOOLS: dict[str, Tool] = {
    t.name: t
    for t in (
        _from_module(run_scan),
        _from_module(read_file),
        _from_module(run_tests),
        _from_module(edit_file, writes=True),
    )
}


@dataclass(frozen=True)
class ToolResult:
    tool: str
    ok: bool
    content: str
    error_type: str | None
    duration_ms: float


def tool_definitions() -> list[dict]:
    """Tool specs in the shape the model API expects."""
    return [
        {"name": t.name, "description": t.description, "input_schema": t.input_schema}
        for t in TOOLS.values()
    ]


def _execute(name: str, args, ctx: ToolContext) -> tuple[bool, str, str | None]:
    tool = TOOLS.get(name)
    if tool is None:
        available = ", ".join(TOOLS)
        return False, f"ERROR: unknown tool '{name}'. Available tools: {available}.", "unknown_tool"
    problems = validate(tool.input_schema, args)
    if problems:
        return (
            False,
            f"ERROR ({name}): invalid arguments: {'; '.join(problems)}. Fix the arguments and call again.",
            "bad_arguments",
        )
    try:
        return True, tool.run(args, ctx), None
    except ToolError as e:
        return False, f"ERROR ({name}): {e.message}", e.kind
    except Exception as e:  # never leak a stack trace to the model
        return (
            False,
            f"ERROR ({name}): internal tool failure ({type(e).__name__}). "
            "This was not caused by your arguments. You may retry once.",
            "internal",
        )


def call_tool(
    name: str,
    args,
    ctx: ToolContext,
    logger: CallLogger | None = None,
    step: int | None = None,
) -> ToolResult:
    started = datetime.now(timezone.utc).isoformat()
    t0 = time.perf_counter()
    ok, content, error_type = _execute(name, args, ctx)
    duration_ms = round((time.perf_counter() - t0) * 1000, 1)
    if logger is not None:
        logger.log(
            {
                "ts": started,
                "step": step,
                "tool": name,
                "input": args,
                "ok": ok,
                "error_type": error_type,
                "output": content[:MAX_LOGGED_OUTPUT],
                "output_chars": len(content),
                "duration_ms": duration_ms,
            }
        )
    return ToolResult(name, ok, content, error_type, duration_ms)
