"""RulesModel: a deterministic, free stand-in for the LLM behind the same Model interface.

No API calls, no cost. It reads the conversation the loop already keeps (the
same messages GPT would see), rebuilds what happened, and picks one tool per
turn by fixed rules:

  scan -> read requirements.txt -> bump each vulnerable pin to the smallest
  version that fixes every finding for it -> run tests -> rescan.

When it cannot finish safely (no fixed version, the bump breaks the tests, a
tool keeps failing, findings in files it cannot edit) it files one issue with
create_issue so a human decides. It is the baseline the eval compares GPT to,
and what `python -m agent` uses when no model is named.
"""

from __future__ import annotations

import json
from dataclasses import dataclass

from packaging.version import InvalidVersion, Version

from agent.model import ModelReply, ToolCall

EDITABLE = "requirements.txt"


@dataclass
class Step:
    tool: str
    args: dict
    ok: bool
    content: str

    @property
    def data(self) -> dict | None:
        if not self.ok:
            return None
        try:  # tool output may carry a trailing recovery note after the JSON
            value, _ = json.JSONDecoder().raw_decode(self.content.lstrip())
        except json.JSONDecodeError:
            return None
        return value if isinstance(value, dict) else None


def transcript(messages: list[dict]) -> list[Step]:
    """Rebuild (tool, args, result) steps from OpenAI-format messages."""
    steps, pending = [], {}
    for m in messages:
        if m.get("role") == "assistant":
            for c in m.get("tool_calls") or []:
                fn = c.get("function") or {}
                try:
                    args = json.loads(fn.get("arguments") or "{}")
                except json.JSONDecodeError:
                    args = {}
                pending[c.get("id")] = (fn.get("name"), args if isinstance(args, dict) else {})
        elif m.get("role") == "tool" and m.get("tool_call_id") in pending:
            name, args = pending.pop(m["tool_call_id"])
            content = m.get("content") or ""
            steps.append(Step(name, args, not content.startswith("ERROR"), content))
    return steps


def _versions(raw: str | None) -> list[Version]:
    out = []
    for part in (raw or "").split(","):
        try:
            out.append(Version(part.strip()))
        except InvalidVersion:
            continue
    return sorted(out)


def fix_version(installed: str | None, fixed: str | None) -> Version | None:
    """Smallest listed fix above the installed version, preferring the same major."""
    try:
        current = Version(installed or "")
    except InvalidVersion:
        current = None
    candidates = [v for v in _versions(fixed) if current is None or v > current]
    if not candidates:
        return None
    same_major = [v for v in candidates if current is not None and v.major == current.major]
    return (same_major or candidates)[0]


def plan_fixes(scan: dict) -> dict[str, str]:
    """package -> version fixing every fixable requirements.txt finding for it."""
    plan: dict[str, Version] = {}
    for v in scan.get("vulnerabilities") or []:
        if (v.get("file") or EDITABLE).replace("\\", "/") != EDITABLE or not v.get("package"):
            continue
        target = fix_version(v.get("installed"), v.get("fixed"))
        if target is not None:
            plan[v["package"]] = max(plan.get(v["package"], target), target)
    return {pkg: str(ver) for pkg, ver in sorted(plan.items())}


def _last(steps: list[Step], pred) -> int:
    for i in range(len(steps) - 1, -1, -1):
        if pred(steps[i]):
            return i
    return -1


class RulesModel:
    name = "rules"

    def __init__(self):
        self.calls = 0

    def _call(self, tool: str, args: dict, why: str) -> ModelReply:
        self.calls += 1
        return ModelReply(text=why, tool_calls=[ToolCall(f"rules_{self.calls}", tool, json.dumps(args))])

    def _issue(self, title: str, body: str) -> ModelReply:
        return self._call("create_issue", {"title": title[:120].rstrip(), "body": body[:3900]},
                          "I cannot finish this safely; handing it to a human.")

    def complete(self, messages: list[dict], tools: list[dict]) -> ModelReply:
        steps = transcript(messages)
        last = steps[-1] if steps else None

        if last is not None and not last.ok:
            if last.tool == "create_issue":
                return ModelReply(text="Filing the issue failed too; stopping.")
            first_line = last.content.splitlines()[0][:300] if last.content else ""
            return self._issue(f"PatchPilot stopped: {last.tool} failed",
                               f"`{last.tool}` failed and could not be recovered:\n\n> {first_line}\n\n"
                               "A human needs to look at this.")

        i_scan = _last(steps, lambda s: s.tool == "run_scan" and s.data is not None)
        if i_scan < 0:
            return self._call("run_scan", {}, "Scan first to see what is vulnerable.")
        scan = steps[i_scan].data or {}

        i_edit = _last(steps, lambda s: s.tool == "edit_file" and (s.data or {}).get("changed"))
        i_tests = _last(steps, lambda s: s.tool == "run_tests" and s.data is not None)
        tests = steps[i_tests].data if i_tests >= 0 else None

        if i_edit > i_tests:
            return self._call("run_tests", {}, "An edit landed: run the tests.")
        if tests is not None and i_tests > i_edit >= 0 and not tests.get("green"):
            return self._issue(
                "Bumping a vulnerable pin breaks the tests",
                f"PatchPilot bumped a pin to fix a vulnerability, but the tests failed, so the edit was rolled back.\n\n"
                f"Test summary: {tests.get('summary') or 'n/a'}\n\n```\n{(tests.get('output_tail') or '')[-1500:]}\n```\n\n"
                "A human needs to decide: adapt the code to the new version, or accept the risk.")
        if i_edit > i_scan:
            return self._call("run_scan", {}, "Tests are green after the fix: rescan to confirm.")

        vulns = scan.get("vulnerabilities") or []
        if scan.get("clean"):
            if tests is None:
                return self._call("run_tests", {}, "Scan is clean: confirm the tests pass.")
            return self._issue("Tests fail on the unchanged repository",
                               f"The scan is clean, but the repository's own tests fail before any change.\n\n"
                               f"Test summary: {tests.get('summary') or 'n/a'}")

        plan = plan_fixes(scan)
        tried = {(s.args.get("package"), s.args.get("version")) for s in steps if s.tool == "edit_file"}
        for pkg, ver in plan.items():
            if (pkg, ver) in tried:
                continue
            i_read = _last(steps, lambda s: s.tool == "read_file" and s.ok)
            if i_read < 0 or i_read < i_edit:
                return self._call("read_file", {"path": EDITABLE}, "Read the pins before changing one.")
            return self._call("edit_file", {"path": EDITABLE, "package": pkg, "version": ver},
                              f"Bump {pkg} to {ver}, the smallest version that fixes every finding for it.")

        rows = "\n".join(
            f"| {v.get('id')} | {v.get('package')} | {v.get('installed')} | {v.get('fixed') or 'none'} | {v.get('file')} |"
            for v in vulns[:30])
        no_fix = all(v.get("fixed") is None for v in vulns)
        title = ("No fixed version available for " + ", ".join(sorted({v.get('package') or '?' for v in vulns}))
                 if no_fix else "Vulnerabilities PatchPilot cannot fix by itself")
        why = ("no fixed version exists" if no_fix else
               "the remaining findings have no fix PatchPilot can apply (other files than requirements.txt, "
               "or the fix it applied did not clear them)")
        return self._issue(title, f"PatchPilot stopped because {why}.\n\n"
                                  "| ID | Package | Installed | Fixed in | File |\n| --- | --- | --- | --- | --- |\n"
                                  f"{rows}\n\nA human needs to decide the next step.")
