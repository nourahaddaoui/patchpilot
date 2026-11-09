"""The human checkpoint: show the write the agent wants to do, wait for yes or no.

edit_file calls ctx.approve(proposal) before it writes anything. The Checkpoint
is that approver. It shows:

  1. the full unified diff of requirements.txt,
  2. the scan BEFORE (the repo as it is now),
  3. the scan AFTER (a real scan of the proposed requirements.txt, run on a
     temporary copy, so nothing in the repo changes while you decide),
  4. what the edit fixes, what stays open, and anything it would introduce,

then asks "Apply this edit? [y/n]". Anything but an explicit yes is a no; a
closed input (no human at the terminal) is a no. A no makes edit_file refuse
with approval_denied, and the stop rule ends the run.

Every decision is logged (event "checkpoint") with the proposal, both scans,
the answer and how long the human took, so the grader can check that no write
ever happened without a recorded yes.

Scanner text (CVE titles) is untrusted, so control characters are stripped
before anything is printed.
"""

from __future__ import annotations

import json
import re
import tempfile
import time
from dataclasses import replace
from pathlib import Path
from typing import Callable

from agent.calllog import CallLogger
from agent.tools import run_scan
from agent.tools.context import ToolContext
from agent.tools.errors import ToolError

YES = {"y", "yes"}
NO = {"n", "no"}
_CONTROL = re.compile(r"[\x00-\x08\x0b-\x1f\x7f-\x9f]")
MAX_TITLE = 90


def clean(text, limit: int | None = None) -> str:
    """Make untrusted text safe to print on one terminal line."""
    s = _CONTROL.sub("", str(text if text is not None else "")).replace("\n", " ")
    return s if limit is None or len(s) <= limit else s[: limit - 1] + "…"


def scan_text(ctx: ToolContext, requirements: str | None = None) -> dict:
    """Scan the repo, or (if `requirements` is given) a temp folder holding only that requirements.txt.

    Returns run_scan's summary dict, or {"error": "..."} if the scanner is unavailable.
    """
    try:
        if requirements is None:
            return json.loads(run_scan.run({}, ctx))
        with tempfile.TemporaryDirectory(prefix="patchpilot-preview-") as tmp:
            (Path(tmp) / "requirements.txt").write_text(requirements, encoding="utf-8")
            return json.loads(run_scan.run({}, replace(ctx, repo_root=Path(tmp))))
    except ToolError as e:
        return {"error": e.message}
    except Exception as e:  # the checkpoint must never crash the run; show it and still ask
        return {"error": f"scan failed ({type(e).__name__})"}


def _ids(scan: dict) -> dict[str, dict]:
    return {v["id"]: v for v in scan.get("vulnerabilities") or [] if v.get("id")}


def compare(before: dict, after: dict) -> dict:
    """Which findings the edit fixes, leaves open, or introduces."""
    if "error" in before or "error" in after:
        return {"fixed": [], "still_open": [], "introduced": [], "available": False}
    b, a = _ids(before), _ids(after)
    return {
        "fixed": sorted(set(b) - set(a)),
        "still_open": sorted(set(b) & set(a)),
        "introduced": sorted(set(a) - set(b)),
        "available": True,
    }


def render(proposal: dict, before: dict, after: dict) -> str:
    """The text the human sees. Pure function, so it is easy to test."""
    lines = [
        "",
        "=" * 72,
        f" PatchPilot wants to edit {clean(proposal['path'])}",
        f" {clean(proposal['package'])}: {clean(proposal['from'])} -> {clean(proposal['to'])} ({clean(proposal['action'])})",
        "=" * 72,
        "",
        "--- Diff " + "-" * 63,
        clean_block(proposal.get("diff", "")),
    ]

    for label, scan in (("Scan BEFORE (repo now)", before), ("Scan AFTER (if you approve)", after)):
        lines.append(f"--- {label} " + "-" * max(0, 67 - len(label)))
        if "error" in scan:
            lines.append(f"  scan unavailable: {clean(scan['error'], 200)}")
        elif scan.get("clean"):
            lines.append("  clean: no known vulnerabilities")
        else:
            lines.append(f"  {scan.get('count', 0)} known vulnerabilities")
            for v in scan.get("vulnerabilities") or []:
                fixed = clean(v.get("fixed")) if v.get("fixed") else "no fix"
                lines.append(
                    f"  {clean(v.get('id')):<16} {clean(v.get('package'))} {clean(v.get('installed'))}"
                    f"  [{clean(v.get('severity'))}] fixed in {fixed}  {clean(v.get('title'), MAX_TITLE)}"
                )
        lines.append("")

    diff = compare(before, after)
    lines.append("--- Effect " + "-" * 61)
    if not diff["available"]:
        lines.append("  unknown: the scanner is unavailable, judge from the diff alone")
    else:
        def ids(xs: list[str]) -> str:
            return clean(", ".join(xs), 400)

        lines.append(f"  fixes      : {ids(diff['fixed']) or 'nothing'}")
        lines.append(f"  still open : {ids(diff['still_open']) or 'none'}")
        if diff["introduced"]:
            lines.append(f"  INTRODUCES : {ids(diff['introduced'])}   <-- new vulnerabilities")
    lines.append("")
    return "\n".join(lines)


def clean_block(text: str) -> str:
    return "\n".join(_CONTROL.sub("", line) for line in (text or "").splitlines())


class Checkpoint:
    """The approver passed as ToolContext.approve.

    ask:  reads the human's answer (default: input). Raising EOFError means "no".
    show: prints the checkpoint text (default: print).
    """

    def __init__(
        self,
        ctx: ToolContext,
        logger: CallLogger | None = None,
        ask: Callable[[str], str] = input,
        show: Callable[[str], None] = print,
        max_prompts: int = 3,
    ):
        self.ctx = ctx
        self.logger = logger
        self.ask = ask
        self.show = show
        self.max_prompts = max_prompts
        self.decisions: list[dict] = []

    def _answer(self) -> tuple[bool, str]:
        for _ in range(self.max_prompts):
            try:
                raw = self.ask("Apply this edit? [y/n] ")
            except (EOFError, KeyboardInterrupt):
                return False, "<no input>"
            answer = (raw or "").strip().lower()
            if answer in YES:
                return True, answer
            if answer in NO:
                return False, answer
            self.show("Please answer y or n.")
        return False, "<no valid answer>"  # unclear means no

    def __call__(self, proposal: dict) -> bool:
        before = scan_text(self.ctx)
        after = scan_text(self.ctx, proposal["new_text"]) if "new_text" in proposal else {
            "error": "proposal has no file contents to preview"}
        self.show(render(proposal, before, after))

        t0 = time.perf_counter()
        approved, answer = self._answer()
        waited_ms = round((time.perf_counter() - t0) * 1000, 1)
        self.show("Approved: writing the edit." if approved else "Denied: nothing was written; the run will stop.")

        effect = compare(before, after)
        decision = {
            "event": "checkpoint",
            "approved": approved,
            "answer": answer,
            "waited_ms": waited_ms,
            "proposal": {k: proposal.get(k) for k in ("path", "package", "action", "from", "to", "diff")},
            "scan_before": sorted(_ids(before)) if "error" not in before else {"error": before["error"]},
            "scan_after": sorted(_ids(after)) if "error" not in after else {"error": after["error"]},
            "fixes": effect["fixed"],
            "introduces": effect["introduced"],
        }
        self.decisions.append(decision)
        if self.logger is not None:
            self.logger.log(decision)
        return approved


class ScriptedHuman:
    """A stand-in human for evals: answers from a list, then "no" when it runs out.

    ScriptedHuman(["y"])       approves once
    ScriptedHuman(["n"])       denies
    ScriptedHuman(["y", "n"])  approves the first edit, changes their mind on the second
    """

    def __init__(self, answers: list[str]):
        self.answers = list(answers)
        self.prompts = 0

    def __call__(self, prompt: str) -> str:
        self.prompts += 1
        if not self.answers:
            raise EOFError
        return self.answers.pop(0)
