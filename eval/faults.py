"""Fault injection: make a chosen tool fail on a chosen call.

A Fault sits in the fake world (eval/world.py) between a tool and the program it
runs (trivy, pip or pytest). The REAL tool code still runs and turns the failure
into its normal plain-text error, so the eval exercises the same error handling,
retry and recovery as a real run.

    Fault("trivy", on_call=1, kind="timeout")            # first scan times out once
    Fault("pytest", on_call=1, kind="timeout", times=2)  # tests time out twice (retry also fails)
    Fault("trivy", on_call=1, kind="down", times=0)      # scanner missing for the whole run

Calls are counted per program, from 1. The checkpoint also runs trivy (scan before
and after a proposed edit), so in a fix run trivy call 1 is the first scan, 2 and 3
are the checkpoint's preview, 4 is the rescan.

Kinds and the error each one produces in the tool:
    timeout     subprocess.TimeoutExpired     -> "timeout"     (retried once)
    bad_output  exit 0 with output that is not JSON -> "bad_output" (trivy only; retried once)
    down        FileNotFoundError (program missing) -> "tool_down" (not retried)
    crash       exit code 2 with an error message   -> "tool_down" for trivy; for pytest a red run
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass

KINDS = ("timeout", "bad_output", "down", "crash")
PROGRAMS = ("trivy", "pip", "pytest")


@dataclass(frozen=True)
class Fault:
    program: str  # trivy | pip | pytest
    on_call: int = 1  # the first failing call of that program, counted from 1
    kind: str = "timeout"
    times: int = 1  # consecutive failing calls; 0 = every call from on_call on

    def __post_init__(self):
        if self.program not in PROGRAMS:
            raise ValueError(f"unknown program '{self.program}': use one of {PROGRAMS}")
        if self.kind not in KINDS:
            raise ValueError(f"unknown fault kind '{self.kind}': use one of {KINDS}")
        if self.on_call < 1 or self.times < 0:
            raise ValueError("on_call starts at 1 and times is >= 0")

    def hits(self, program: str, call_no: int) -> bool:
        if program != self.program or call_no < self.on_call:
            return False
        return self.times == 0 or call_no < self.on_call + self.times

    def fire(self, cmd: list[str], timeout: float | None) -> subprocess.CompletedProcess:
        """Raise or return what the failing program would. Called only when hits() is true."""
        if self.kind == "timeout":
            raise subprocess.TimeoutExpired(cmd, timeout or 0)
        if self.kind == "down":
            raise FileNotFoundError(cmd[0])
        if self.kind == "bad_output":
            return subprocess.CompletedProcess(cmd, 0, "<<garbled scanner output>>", "")
        return subprocess.CompletedProcess(cmd, 2, "", f"{self.program}: internal error (injected fault)\n")

    def describe(self) -> str:
        span = "every call" if self.times == 0 else ("once" if self.times == 1 else f"{self.times} calls")
        return f"{self.program} {self.kind} from call {self.on_call}, {span}"
