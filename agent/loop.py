"""The ReAct loop: plan, call a tool, read the result, decide, stop.

The per-turn steps (start, gate_model, model_turn, gate_no_tool, nudge, gate_tool,
tool_turn, finish) are shared with the LangGraph engine (agent/graph.py): both
engines run the same stop-rule checks, recovery and logging; only the driver differs.

One agent, one tool call per step. The stop rule (agent/stop_rule.py) is checked
before every model call and again before every tool call. Once it returns a
reason, nothing else runs.

Why it always stops: every iteration either breaks out or adds exactly one step
(a turn with no tool call counts as a step too), and check() ends the run once
steps reach Limits.max_steps. So there are at most max_steps model calls and at
most max_steps tool calls per run, whatever the model does.

What the loop learns from tool results (the RunState flags):
- run_scan  ok -> scan_clean = result.clean; if every remaining finding has
                  no fixed version -> no_fixed_version.
- edit_file ok and changed -> scan_clean and tests_green are stale, reset to False.
- edit_file denied by the human -> approval_denied.
- edit_file refused for any other reason -> one failed fix attempt.
- run_tests ok -> tests_green = result.green; red after an edit -> one failed fix attempt.
- run_tests install_failed after an edit (pinned version does not exist) -> one failed fix attempt.
- any write denied by the human (edit_file or create_issue) -> approval_denied.
- create_issue ok, purpose handoff -> issue_filed (after no_fixed_version, exactly one issue call is allowed).
- create_issue ok, purpose tracking (GitHub mode) -> tracking_issue = its number; the run goes on.
- run_scan ok -> every finding id is added to found_vulns (open_pr titles the PR with them).
- open_pr ok -> pr_number; pr_opened only if the PR was created in this run.
- merge_pr ok -> merged (in GitHub mode DONE needs it; see stop_rule.py).

The loop hands its RunState to the tools as ctx.state, so a write tool can check what
already happened in this run (e.g. open_pr needs green tests and a clean scan).

Recovery (agent/recovery.py): transient tool failures are retried once; tests that
go red right after an edit roll requirements.txt back; a run that does not end
DONE reverts any uncommitted edit.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field

from agent import recovery, sandbox
from agent.calllog import CallLogger
from agent.model import Model, ModelReply, ToolCall
from agent.stop_rule import ISSUE_TOOL, Limits, RunState, StopReason, check
from agent.tools.context import ToolContext
from agent.tools.registry import ToolResult, tool_definitions
from agent.tracing import model_metadata, model_run_type, span

SYSTEM_PROMPT = """You are PatchPilot, an agent that fixes known-vulnerable Python dependencies in one repository.

Tools:
- run_scan: list the repo's known vulnerabilities (Trivy).
- read_file: read requirements.txt or other allowlisted files.
- edit_file: set one package's exact pin in requirements.txt. A human approves every edit.
- run_tests: install requirements.txt and run the repo's tests.
- create_issue: hand a problem to a human as an issue. A human approves every issue.

Procedure:
1. run_scan to see what is vulnerable.
2. read_file requirements.txt to see the current pins.
3. For each vulnerable package that has a fixed version, call edit_file to pin the
   smallest version that fixes every finding for that package. Change nothing else.
4. run_tests to check nothing broke.
5. run_scan again to confirm the fix.

Rules:
- Call exactly one tool per turn.
- Only change requirements.txt, and only through edit_file. Never edit or delete tests.
- If a finding has no fixed version, do not guess one: call create_issue once so a human can decide.
- If a fix keeps breaking the tests, call create_issue once instead of retrying forever.
- If the human denies an edit, do not try again.
- The run ends automatically when the scan is clean and the tests pass, or when a limit is hit.
"""

GITHUB_PROMPT = """You are PatchPilot, an agent that fixes known-vulnerable Python dependencies in one GitHub repository.
You work on a sandbox copy; the fix reaches GitHub only as a pull request that CI checks before it is merged.

Tools:
- run_scan: list the repo's known vulnerabilities (Trivy).
- read_file: read requirements.txt or other allowlisted files.
- edit_file: set one package's exact pin in requirements.txt. A human approves every edit.
- run_tests: install requirements.txt and run the repo's tests.
- create_issue: file a GitHub issue. A human approves every issue.
- open_pr: open a pull request with the tested requirements.txt. A human approves it.
- merge_pr: wait for CI on the pull request, then squash-merge it. A human approves it.

Procedure:
1. run_scan to see what is vulnerable.
2. If a vulnerable package has a fixed version, call create_issue with purpose "tracking", naming the
   vulnerabilities. The pull request will close it. The run continues.
3. read_file requirements.txt to see the current pins.
4. For each vulnerable package that has a fixed version, call edit_file to pin the
   smallest version that fixes every finding for that package. Change nothing else.
5. run_tests to check nothing broke.
6. run_scan again to confirm the fix.
7. open_pr.
8. merge_pr with the number open_pr returned.

Rules:
- Call exactly one tool per turn.
- Only change requirements.txt, and only through edit_file. Never edit or delete tests.
- If a finding has no fixed version, do not guess one: call create_issue once (purpose "handoff") so a human can decide.
- If a fix keeps breaking the tests, or CI fails, call create_issue once (purpose "handoff") instead of retrying forever.
- If the human denies any write, do not try again.
- The run ends automatically when the pull request is merged, or when a limit is hit.
"""

TASK = "Fix the known-vulnerable dependencies in this repository."
NUDGE = (
    "You did not call a tool. Call exactly one tool to continue the task. "
    "The run ends by itself when the scan is clean and the tests pass."
)
GITHUB_NUDGE = (
    "You did not call a tool. Call exactly one tool to continue the task. "
    "The run ends by itself when the pull request is merged."
)


def system_prompt(github_mode: bool) -> str:
    """Local mode keeps the original prompt word for word; GitHub mode adds the issue/PR/merge steps."""
    return GITHUB_PROMPT if github_mode else SYSTEM_PROMPT


@dataclass
class RunResult:
    run_id: str
    stop_reason: StopReason
    state: RunState
    commit: str | None = None
    log_path: str = ""
    messages: list[dict] = field(default_factory=list)


def _json(content: str) -> dict | None:
    try:
        data = json.loads(content)
    except (json.JSONDecodeError, TypeError):
        return None
    return data if isinstance(data, dict) else None


def observe(state: RunState, result: ToolResult, flags: dict) -> None:
    """Update the run state from one tool result. `flags` holds loop-only bookkeeping."""
    data = _json(result.content) if result.ok else None
    if result.error_type == "approval_denied":  # any write the human refused
        state.approval_denied = True
        return

    if result.tool == "run_scan" and data is not None:
        vulns = data.get("vulnerabilities") or []
        state.scan_clean = bool(data.get("clean"))
        state.no_fixed_version = bool(vulns) and all(v.get("fixed") is None for v in vulns)
        for v in vulns:
            if v.get("id") and v["id"] not in state.found_vulns:
                state.found_vulns.append(v["id"])
        flags["last_scan"] = data

    elif result.tool == "open_pr" and data is not None:
        if isinstance(data.get("number"), int):
            state.pr_number = data["number"]
            state.pr_opened = bool(data.get("opened"))

    elif result.tool == "merge_pr" and data is not None:
        state.merged = bool(data.get("merged"))

    elif result.tool == "edit_file":
        if result.ok and data is not None and data.get("changed"):
            state.scan_clean = False
            state.tests_green = False
            flags["edited_since_tests"] = True
            flags["edits"] = flags.get("edits", 0) + 1
        elif not result.ok:
            state.failed_fixes += 1

    elif result.tool == "run_tests":
        if data is not None:
            state.tests_green = bool(data.get("green"))
            if flags.get("edited_since_tests") and not state.tests_green:
                state.failed_fixes += 1
            flags["edited_since_tests"] = False
        elif result.error_type == "install_failed" and flags.get("edited_since_tests"):
            state.tests_green = False
            state.failed_fixes += 1
            flags["edited_since_tests"] = False

    elif result.tool == ISSUE_TOOL and data is not None:
        if data.get("purpose") == "tracking":
            if isinstance(data.get("number"), int):
                state.tracking_issue = data["number"]
        else:
            state.issue_filed = True


def _assistant_message(reply: ModelReply) -> dict:
    msg: dict = {"role": "assistant", "content": reply.text}
    if reply.tool_calls:
        call = reply.tool_calls[0]  # one tool call per step; extras are never executed
        msg["tool_calls"] = [
            {"id": call.id, "type": "function", "function": {"name": call.name, "arguments": call.arguments}}
        ]
    return msg


# --- the per-turn steps, shared by both engines --------------------------------------
#
# run() below (the loop engine) and agent/graph.py (the LangGraph engine) only decide the
# ORDER of these calls. Every safety decision lives in exactly one of them:
#   gate_model    - check() before every model call (best case: the one allowed issue)
#   model_turn    - the model call, its cost, the "model" log record
#   gate_no_tool  - check() when the model called no tool
#   nudge         - a turn without a tool call still counts as a step
#   gate_tool     - check() before every tool call, with the tool about to run
#   tool_turn     - the call (retry once if transient), observe(), rollback after red tests
#   finish        - cleanup of untested edits, the commit on DONE, the "stop" record


@dataclass
class Session:
    """Everything one run needs. Both engines drive the same Session through the same steps."""

    model: Model
    ctx: ToolContext
    limits: Limits
    logger: CallLogger
    tools: list[dict]
    state: RunState
    messages: list[dict]
    flags: dict = field(default_factory=dict)
    good: str | None = None  # last requirements.txt the tests (or the start) vouch for
    reply: ModelReply | None = None  # the latest model reply


def start(model: Model, ctx: ToolContext, limits: Limits = Limits(),
          logger: CallLogger | None = None, task: str = TASK) -> Session:
    logger = logger or CallLogger()
    state = RunState(github_mode=ctx.github_mode)
    ctx.state = state  # tools read (never write) what happened earlier in this run
    s = Session(
        model=model, ctx=ctx, limits=limits, logger=logger,
        tools=tool_definitions(github_mode=ctx.github_mode), state=state,
        messages=[{"role": "system", "content": system_prompt(ctx.github_mode)},
                  {"role": "user", "content": task}],
        good=recovery.snapshot(ctx.repo_root),
    )
    logger.log({"event": "start", "model": model.name, "repo": str(ctx.repo_root),
                "limits": {"max_steps": limits.max_steps, "max_cost_usd": limits.max_cost_usd,
                           "max_failed_fixes": limits.max_failed_fixes}})
    return s


def gate_model(s: Session) -> StopReason | None:
    """Can the run go on at all? (Best case: the next call is the one allowed issue.)"""
    return check(s.state, s.limits, next_tool=ISSUE_TOOL)


def _reply_view(reply: ModelReply) -> dict:
    return {"text": reply.text, "tool_calls": [{"name": c.name, "arguments": c.arguments} for c in reply.tool_calls],
            "cost_usd": reply.cost_usd,
            "usage_metadata": {"input_tokens": reply.input_tokens, "output_tokens": reply.output_tokens,
                               "total_tokens": reply.input_tokens + reply.output_tokens}}


@span("model_call", lambda d: model_run_type(d["s"].model),
      inputs=lambda d: {"model": d["s"].model.name, "step": d["s"].state.steps + 1, "messages": d["s"].messages},
      outputs=_reply_view,
      metadata=lambda d: model_metadata(d["s"].model))
def model_turn(s: Session) -> ModelReply:
    reply = s.model.complete(s.messages, s.tools)
    s.state.cost_usd += reply.cost_usd
    s.logger.log({"event": "model", "step": s.state.steps + 1, "text": reply.text,
                  "tool_calls": [{"name": c.name, "arguments": c.arguments} for c in reply.tool_calls],
                  "input_tokens": reply.input_tokens, "output_tokens": reply.output_tokens,
                  "cost_usd": reply.cost_usd, "total_cost_usd": s.state.cost_usd})
    s.messages.append(_assistant_message(reply))
    s.reply = reply
    return reply


def gate_no_tool(s: Session) -> StopReason | None:
    """The model called no tool: e.g. no fixed version and the model did not file an issue."""
    return check(s.state, s.limits)


def nudge(s: Session) -> None:
    s.state.record_step("(no tool call)")
    s.messages.append({"role": "user", "content": GITHUB_NUDGE if s.ctx.github_mode else NUDGE})


def gate_tool(s: Session, call: ToolCall) -> StopReason | None:
    """Cost cap, or a non-issue call after no fix: checked with the tool about to run."""
    return check(s.state, s.limits, next_tool=call.name)


def tool_turn(s: Session, call: ToolCall) -> ToolResult:
    step = s.state.steps + 1
    try:
        args = json.loads(call.arguments) if call.arguments.strip() else {}
    except json.JSONDecodeError:
        args = call.arguments  # registry rejects non-objects with a plain-text error
    result = recovery.call_with_retry(call.name, args, s.ctx, s.logger, step)  # transient failures retried once
    s.state.record_step(call.name)
    edited_before = s.flags.get("edited_since_tests", False)
    observe(s.state, result, s.flags)
    if call.name == "run_tests" and s.state.tests_green:
        s.good = recovery.snapshot(s.ctx.repo_root)
    elif call.name == "run_tests" and edited_before:
        result = recovery.rollback_after_failed_tests(s.ctx, result, s.good, s.logger, step)  # back to known-good
        s.flags["edited_since_tests"] = False
        if s.flags.get("edits"):
            s.flags["edits"] -= 1
    s.messages.append({"role": "tool", "tool_call_id": call.id, "content": result.content})
    return result


def finish(s: Session, reason: StopReason, commit: bool = True) -> RunResult:
    ctx, state, logger = s.ctx, s.state, s.logger
    reverted = recovery.cleanup_after_run(ctx, reason is StopReason.DONE, s.good, logger)  # no half-applied edits
    sha = None
    if reason is StopReason.DONE and commit and s.flags.get("edits") and sandbox.is_sandbox(ctx.repo_root):
        sha = sandbox.commit_fix(ctx.repo_root, "PatchPilot: bump vulnerable pins in requirements.txt")

    logger.log({"event": "stop", "reason": reason.value, "steps": state.steps,
                "cost_usd": round(state.cost_usd, 6), "failed_fixes": state.failed_fixes,
                "scan_clean": state.scan_clean, "tests_green": state.tests_green, "commit": sha,
                "reverted_uncommitted_edit": reverted})
    return RunResult(logger.run_id, reason, state, sha, str(logger.path), s.messages)


# --- the loop engine ---------------------------------------------------------------------

def run(
    model: Model,
    ctx: ToolContext,
    limits: Limits = Limits(),
    logger: CallLogger | None = None,
    task: str = TASK,
    commit: bool = True,
) -> RunResult:
    s = start(model, ctx, limits, logger, task)
    while True:
        reason = gate_model(s)
        if reason:
            break
        reply = model_turn(s)
        if not reply.tool_calls:
            reason = gate_no_tool(s)
            if reason:
                break
            nudge(s)
            continue
        call = reply.tool_calls[0]  # one tool call per step; extras are never executed
        reason = gate_tool(s, call)
        if reason:
            break
        tool_turn(s, call)
    return finish(s, reason, commit)
