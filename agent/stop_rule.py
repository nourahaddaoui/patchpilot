"""Stop rule for the PatchPilot agent loop.

SPEC (written before any agent code)
------------------------------------
A run ends on the FIRST of these conditions, checked in this priority order:

1. APPROVAL_DENIED   - the human rejected the proposed diff at the checkpoint.
2. NO_FIXED_VERSION  - the scanner reports no fixed version for the vulnerability.
3. DONE              - the scan is clean AND the tests are green (both required).
4. FIX_ATTEMPTS      - 3 fix attempts have failed.
5. COST_CAP          - accumulated model cost has reached $0.50.
6. STEP_LIMIT        - 12 tool-call steps have been taken.

The loop calls check() before every tool call. The loop may never make another
tool call once check() returns a reason. The returned reason is logged as the
run's named stop reason.

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
    history: list[dict] = field(default_factory=list)

    def record_step(self, tool_name: str, cost_usd: float = 0.0) -> None:
        """Count one tool call and add its cost."""
        self.steps += 1
        self.cost_usd += cost_usd
        self.history.append(
            {"step": self.steps, "tool": tool_name, "cost_usd": cost_usd}
        )


def check(state: RunState, limits: Limits = Limits()) -> StopReason | None:
    """Return the stop reason if the run must end now, else None."""
    if state.approval_denied:
        return StopReason.APPROVAL_DENIED
    if state.no_fixed_version:
        return StopReason.NO_FIXED_VERSION
    if state.scan_clean and state.tests_green:
        return StopReason.DONE
    if state.failed_fixes >= limits.max_failed_fixes:
        return StopReason.FIX_ATTEMPTS
    if state.cost_usd >= limits.max_cost_usd:
        return StopReason.COST_CAP
    if state.steps >= limits.max_steps:
        return StopReason.STEP_LIMIT
    return None
