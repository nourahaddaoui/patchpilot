"""open_pr (write): open a pull request on GitHub with the tested requirements.txt.

GitHub mode only. Uses the GitHub REST API (contents, refs, pulls); never `git push`,
so the token is never in a git config, a remote URL or a command line.

Safety rules:
- Refuses unless, in THIS run (ctx.state), the tests are green and the last scan is
  clean: only a verified fix leaves the machine.
- Refuses on drift: if requirements.txt on the remote main differs from the sandbox
  baseline, the fix was made against another file and could undo someone's change.
- Commits ONLY requirements.txt, on branch patchpilot/fix (created or reset to main's
  sha), and opens a PR into main. Nothing else in the repo is touched.
- "Fixes #N" is added only when this run opened a tracking issue N; a number is never guessed.
- Idempotent: an open PR from patchpilot/fix is returned instead of opening another.
- Nothing is written until ctx.approve(proposal) returns True. The proposal carries the
  repo, base/head, exact title and body, and the requirements.txt diff.
"""

from __future__ import annotations

import base64
import difflib
import json
import re
import subprocess

from agent.sandbox import FIX_BRANCH, is_sandbox
from agent.tools.context import ToolContext
from agent.tools.create_issue import _REPO, MARK, github_request, neutralize
from agent.tools.errors import ToolError

NAME = "open_pr"
DESCRIPTION = (
    "GitHub mode only. Open a pull request into main with the tested requirements.txt from the sandbox "
    "(branch patchpilot/fix). Call it only after the tests are green and a rescan is clean. A human must "
    "approve the exact title, body and diff first; if they deny it, nothing is pushed and the run must stop. "
    "If a PatchPilot PR is already open it is returned instead. "
    "Returns JSON: opened, existing, number, url, head_sha, title."
)
INPUT_SCHEMA = {
    "type": "object",
    "properties": {
        "notes": {
            "type": "string",
            "maxLength": 1500,
            "description": "Optional extra lines for the PR description (e.g. why this version). Plain markdown.",
        },
    },
    "additionalProperties": False,
}

BASE = "main"
FILE = "requirements.txt"
FOOTER = f"\n\n---\nOpened by {MARK} after human approval."
NOTHING = "Nothing was changed."
MAX_TITLE = 120
_PIN = re.compile(r"^\s*([A-Za-z0-9][A-Za-z0-9._-]*)\s*==\s*([^\s;#]+)")


# --- local facts -------------------------------------------------------------------

def _baseline(ctx: ToolContext) -> str:
    """requirements.txt as the sandbox started (the baseline commit on its main branch)."""
    proc = subprocess.run(["git", "show", f"{BASE}:{FILE}"], cwd=ctx.repo_root, capture_output=True, text=True)
    if proc.returncode != 0:
        raise ToolError("not_found", f"the sandbox has no baseline {FILE} on its main branch. {NOTHING}")
    return proc.stdout


def pins(text: str) -> dict[str, str]:
    out = {}
    for line in text.splitlines():
        m = _PIN.match(line)
        if m:
            out[m.group(1).lower()] = m.group(2)
    return out


def diff_text(old: str, new: str) -> str:
    return "".join(difflib.unified_diff(old.splitlines(True), new.splitlines(True),
                                        f"a/{FILE}", f"b/{FILE}"))


def make_title(cves: list[str], bumps: dict[str, tuple[str, str]]) -> str:
    pkgs = ", ".join(sorted(bumps))
    if not cves:
        return f"PatchPilot: bump {pkgs} in {FILE}"[:MAX_TITLE]
    shown, rest = cves[:3], len(cves) - 3
    title = f"PatchPilot: fix {', '.join(shown)}{f' (+{rest} more)' if rest > 0 else ''} in {pkgs}"
    return title if len(title) <= MAX_TITLE else title[: MAX_TITLE - 1] + "…"


def make_body(cves: list[str], bumps: dict[str, tuple[str, str]], issue: int | None, notes: str) -> str:
    rows = "\n".join(f"| {p} | {a} | {b} |" for p, (a, b) in sorted(bumps.items()))
    lines = [
        f"PatchPilot bumped vulnerable pins in `{FILE}`. Only that file changes.",
        "",
        "| Package | From | To |",
        "| --- | --- | --- |",
        rows,
        "",
        f"Fixes the known vulnerabilities: {', '.join(cves) if cves else 'see the scan'}.",
        "Before opening this PR, in the sandbox: tests green, rescan clean.",
    ]
    if notes.strip():
        lines += ["", neutralize(notes.strip())]
    if issue is not None:
        lines += ["", f"Fixes #{issue}"]
    return "\n".join(lines) + FOOTER


# --- GitHub -------------------------------------------------------------------------

def _gh(ctx, method, path, body=None, allow=()):
    return github_request(ctx, method, f"/repos/{ctx.github_repo}{path}", body, allow=allow, nothing=NOTHING)


def _open_fix_pr(ctx: ToolContext) -> dict | None:
    owner = ctx.github_repo.split("/")[0]
    _, prs = _gh(ctx, "GET", f"/pulls?state=open&base={BASE}&head={owner}:{FIX_BRANCH}")
    for pr in prs if isinstance(prs, list) else []:
        if isinstance(pr, dict) and (pr.get("head") or {}).get("ref") == FIX_BRANCH:
            return pr
    return None


def _remote_file(ctx: ToolContext) -> tuple[str, str]:
    """(text, blob sha) of requirements.txt on the remote main."""
    _, data = _gh(ctx, "GET", f"/contents/{FILE}?ref={BASE}")
    if not isinstance(data, dict) or data.get("encoding") != "base64" or "sha" not in data:
        raise ToolError("bad_output", f"GitHub returned an unexpected response for {FILE} on {BASE}. {NOTHING} You may retry once.")
    return base64.b64decode(data.get("content") or "").decode("utf-8"), data["sha"]


def _main_sha(ctx: ToolContext) -> str:
    _, ref = _gh(ctx, "GET", f"/git/ref/heads/{BASE}")
    sha = ((ref or {}).get("object") or {}).get("sha") if isinstance(ref, dict) else None
    if not sha:
        raise ToolError("bad_output", f"GitHub returned an unexpected response for branch {BASE}. {NOTHING} You may retry once.")
    return sha


def _point_branch_at(ctx: ToolContext, sha: str) -> None:
    """Create patchpilot/fix at `sha`, or move it there (it is PatchPilot's own branch, no PR is open on it)."""
    status, _ = _gh(ctx, "GET", f"/git/ref/heads/{FIX_BRANCH}", allow=(404,))
    if status == 404:
        _gh(ctx, "POST", "/git/refs", {"ref": f"refs/heads/{FIX_BRANCH}", "sha": sha})
    else:
        _gh(ctx, "PATCH", f"/git/refs/heads/{FIX_BRANCH}", {"sha": sha, "force": True})


def _result(pr: dict, existing: bool, head_sha: str | None, title: str) -> str:
    return json.dumps({"opened": not existing, "existing": existing, "number": pr.get("number"),
                       "url": pr.get("html_url"), "head_sha": head_sha or (pr.get("head") or {}).get("sha"),
                       "title": pr.get("title") or title}, ensure_ascii=False)


# --- tool ---------------------------------------------------------------------------

def _check_preconditions(ctx: ToolContext) -> None:
    if not ctx.github_mode:
        raise ToolError("not_allowed", f"open_pr only works in GitHub mode (PATCHPILOT_ISSUES=github). {NOTHING} "
                                       "In local mode the fix is committed in the sandbox when the run ends.")
    if not (ctx.github_repo and _REPO.fullmatch(ctx.github_repo)):
        raise ToolError("not_allowed", f"GitHub mode needs PATCHPILOT_GITHUB_REPO set to 'owner/name'. {NOTHING}")
    if not is_sandbox(ctx.repo_root):
        raise ToolError("not_allowed", f"the target repo is not a sandbox clone; a PR can only come from sandbox/work. {NOTHING}")
    state = ctx.state
    if state is None or not (state.tests_green and state.scan_clean):
        raise ToolError("not_ready", f"a PR needs green tests and a clean rescan in this run, after the last edit. {NOTHING} "
                                     "Call run_tests and run_scan first.")


def run(args: dict, ctx: ToolContext) -> str:
    _check_preconditions(ctx)
    baseline = _baseline(ctx)
    new = (ctx.repo_root / FILE).read_text(encoding="utf-8")
    if new == baseline:
        raise ToolError("not_ready", f"{FILE} is unchanged from the baseline, so there is nothing to propose. {NOTHING}")

    existing = _open_fix_pr(ctx)
    if existing is not None:
        return _result(existing, True, None, existing.get("title") or "")

    remote, blob_sha = _remote_file(ctx)
    if remote != baseline:
        raise ToolError("drift", f"{FILE} on the remote {BASE} changed since the sandbox was made, so this fix "
                                 f"could undo someone else's change. {NOTHING} Stop; a human must reset the sandbox.")

    before, after = pins(baseline), pins(new)
    bumps = {p: (before.get(p, "(new)"), v) for p, v in after.items() if before.get(p) != v}
    cves = sorted(ctx.state.found_vulns)
    issue = ctx.state.tracking_issue
    title = make_title(cves, bumps)
    body = make_body(cves, bumps, issue, args.get("notes", ""))

    if ctx.approve is None:
        raise ToolError("approval_required", f"opening a PR needs human approval and no approver is connected. {NOTHING}")
    proposal = {"tool": NAME, "repo": ctx.github_repo, "base": BASE, "head": FIX_BRANCH,
                "title": title, "body": body, "diff": diff_text(baseline, new), "fixes_issue": issue}
    if not ctx.approve(proposal):
        raise ToolError("approval_denied", f"the human denied this pull request. {NOTHING} Stop the run.")

    _point_branch_at(ctx, _main_sha(ctx))
    _, put = _gh(ctx, "PUT", f"/contents/{FILE}", {
        "message": title, "branch": FIX_BRANCH, "sha": blob_sha,
        "content": base64.b64encode(new.encode("utf-8")).decode("ascii"),
    })
    head_sha = ((put or {}).get("commit") or {}).get("sha") if isinstance(put, dict) else None
    try:
        _, pr = _gh(ctx, "POST", "/pulls", {"title": title, "head": FIX_BRANCH, "base": BASE, "body": body})
    except ToolError as e:
        raise ToolError(e.kind, f"{FILE} was committed on branch {FIX_BRANCH}, but opening the PR failed: "
                                f"{e.message.replace(NOTHING, '').strip()} Calling open_pr again is safe.") from None
    if not isinstance(pr, dict) or "number" not in pr:
        raise ToolError("bad_output", "GitHub accepted the PR but returned an unexpected response; check the repo before retrying.")
    return _result(pr, False, head_sha, title)
