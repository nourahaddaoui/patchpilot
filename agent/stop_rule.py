"""Stop rule for the PatchPilot agent loop.

SPEC (written before any agent code)
------------------------------------
A run ends on the FIRST of these conditions, checked in this priority order:

1. APPROVAL_DENIED   - the human rejected the proposed diff at the checkpoint.
2. NO_FIXED_VERSION  - the scanner reports no fixed version for the vulnerability.
3. DONE              - the scan is clean AND the tests are green (both required).
3b. HANDED_OFF       - the agent filed an issue for a human (create_issue succeeded).
4. FIX_ATTEMPTS      - 3 fix attempts have failed.
5. COST_CAP          - accumulated model cost has reached $0.50.
6. STEP_LIMIT        - 12 tool-call steps have been taken.

The loop calls check() before every tool call, passing the tool it is about to
call. The loop may never make another tool call once check() returns a reason.
The returned reason is logged as the run's named stop reason.

One exception, so a human always hears about an unfixable vulnerability: once
NO_FIXED_VERSION holds, exactly one create_issue call is still allowed (if no
issue was filed yet). Every other limit still applies to that call. After that
issue the reason stays NO_FIXED_VERSION; any other filed issue is HANDED_OFF.
So a run files at most one issue.

Limits are tuned during Week 1 and then frozen.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum


class StopReason(str, Enum):
    APPROVAL_DENIED = "approval_denied"
    NO_FIXED_VERSION = "no_fixed_version"
    DONE = "done"
    FIX_ATTEMPTS = "fix_attempts"
    COST_CAP = "cost_cap"
    STEP_LIMIT = "step_limit"
    HANDED_OFF = "handed_off"


ISSUE_TOOL = "create_issue"


@dataclass(frozen=True)
class Limits:
    max_steps: int = 12
    max_cost_usd: float = 0.50
    max_failed_fixes: int = 3


@dataclass
class RunState:
    steps: int = 0
    cost_usd: float = 0.0
    failed_fixes: int = 0
    scan_clean: bool = False
    tests_green: bool = False
    approval_denied: bool = False
    no_fixed_version: bool = False
    issue_filed: bool = False  # a handoff issue: ends the run
    tracking_issue: int | None = None  # GitHub mode: the issue the PR will close; does not end the run
    found_vulns: list[str] = field(default_factory=list)  # every finding id any scan in this run reported
    pr_number: int | None = None  # GitHub mode: the PR open_pr opened (or found) in this run
    pr_opened: bool = False  # True only if open_pr created that PR in this run (not an older one)
    history: list[dict] = field(default_factory=list)

    def record_step(self, tool_name: str, cost_usd: float = 0.0) -> None:
        """Count one tool call and add its cost."""
        self.steps += 1
        self.cost_usd += cost_usd
        self.history.append(
            {"step": self.steps, "tool": tool_name, "cost_usd": cost_usd}
        )


def check(
    state: RunState, limits: Limits = Limits(), next_tool: str | None = None
) -> StopReason | None:
    """Return the stop reason if the run must end now, else None.

    next_tool is the tool the loop is about to call (None = unknown, strictest).
    """
    if state.approval_denied:
        return StopReason.APPROVAL_DENIED
    if state.no_fixed_version and not (next_tool == ISSUE_TOOL and not state.issue_filed):
        return StopReason.NO_FIXED_VERSION
    if state.scan_clean and state.tests_green:
        return StopReason.DONE
    if state.issue_filed:
        return StopReason.HANDED_OFF
    if state.failed_fixes >= limits.max_failed_fixes:
        return StopReason.FIX_ATTEMPTS
    if state.cost_usd >= limits.max_cost_usd:
        return StopReason.COST_CAP
    if state.steps >= limits.max_steps:
        return StopReason.STEP_LIMIT
    return None
