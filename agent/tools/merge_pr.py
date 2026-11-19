"""merge_pr (write): squash-merge the fix PR once GitHub's CI has passed on it.

GitHub mode only. Uses only endpoints a fine-grained token with Actions (read),
Contents, Issues and Pull requests (write) can call; never an admin endpoint, so it
can never bypass branch protection.

Safety rules:
- Refuses unless the PR is the one open_pr opened in THIS run (not an older one),
  its head branch is patchpilot/fix, and the run's tests are green and scan clean.
- CI must pass on GitHub: it polls the Actions runs for the PR's head sha (every
  ci_poll_s, at most ci_timeout_s) until they have all completed. Any failed run, or
  no completed run in time, is a refusal. Nothing is merged on a guess.
- The merge pins the head sha CI tested (`sha` in the merge call), so a commit pushed
  after CI ran cannot slip in.
- Nothing is merged until ctx.approve(proposal) returns True; the proposal shows the
  PR number, the CI result and exactly what gets merged.
- Idempotent: a PR that is already merged is reported as merged, nothing else is done.
"""

from __future__ import annotations

import json
import time

from agent.sandbox import FIX_BRANCH, is_sandbox
from agent.tools.context import ToolContext
from agent.tools.create_issue import _REPO, github_request
from agent.tools.errors import ToolError

NAME = "merge_pr"
DESCRIPTION = (
    "GitHub mode only. Squash-merge the pull request open_pr opened in this run, after GitHub's CI "
    "has passed on it (this call waits up to 3 minutes for CI). A human must approve the merge first; "
    "if they deny it, nothing is merged and the run must stop. Merging closes the tracking issue. "
    "Returns JSON: merged, already_merged, number, sha, ci."
)
INPUT_SCHEMA = {
    "type": "object",
    "properties": {
        "number": {"type": "integer", "minimum": 1, "description": "The PR number open_pr returned."},
    },
    "required": ["number"],
    "additionalProperties": False,
}

NOTHING = "Nothing was merged."
OK_CONCLUSIONS = {"success", "neutral", "skipped"}

# Indirection so tests can run the poll without waiting.
_sleep = time.sleep
_now = time.monotonic


def _gh(ctx, method, path, body=None, allow=()):
    return github_request(ctx, method, f"/repos/{ctx.github_repo}{path}", body, allow=allow, nothing=NOTHING)


def _check_preconditions(number: int, ctx: ToolContext) -> None:
    if not ctx.github_mode:
        raise ToolError("not_allowed", f"merge_pr only works in GitHub mode (PATCHPILOT_ISSUES=github). {NOTHING}")
    if not (ctx.github_repo and _REPO.fullmatch(ctx.github_repo)):
        raise ToolError("not_allowed", f"GitHub mode needs PATCHPILOT_GITHUB_REPO set to 'owner/name'. {NOTHING}")
    if not is_sandbox(ctx.repo_root):
        raise ToolError("not_allowed", f"the target repo is not a sandbox clone. {NOTHING}")
    state = ctx.state
    if state is None or not (state.tests_green and state.scan_clean):
        raise ToolError("not_ready", f"merging needs green tests and a clean rescan in this run. {NOTHING}")
    if state.pr_number != number or not state.pr_opened:
        raise ToolError("not_allowed", f"PR #{number} was not opened by open_pr in this run; only that PR may be merged. "
                                       f"{NOTHING}")


def ci_status(runs: list[dict]) -> str:
    """'pending' while any run is unfinished (or none exists yet), else 'passed' or 'failed'."""
    if not runs or any(r.get("status") != "completed" for r in runs):
        return "pending"
    conclusions = [r.get("conclusion") for r in runs]
    if any(c not in OK_CONCLUSIONS for c in conclusions) or "success" not in conclusions:
        return "failed"
    return "passed"


def _ci_runs(ctx: ToolContext, sha: str) -> list[dict]:
    _, data = _gh(ctx, "GET", f"/actions/runs?head_sha={sha}&per_page=50")
    runs = (data or {}).get("workflow_runs") if isinstance(data, dict) else None
    if not isinstance(runs, list):
        raise ToolError("bad_output", f"GitHub returned an unexpected response for the CI runs. {NOTHING} You may retry once.")
    return [r for r in runs if isinstance(r, dict)]


def wait_for_ci(ctx: ToolContext, sha: str) -> tuple[str, list[dict]]:
    """Poll until CI is decided or ci_timeout_s passes. Returns (status, runs)."""
    deadline = _now() + ctx.ci_timeout_s
    while True:
        runs = _ci_runs(ctx, sha)
        status = ci_status(runs)
        if status != "pending" or _now() >= deadline:
            return status, runs
        _sleep(ctx.ci_poll_s)


def _ci_summary(runs: list[dict]) -> list[dict]:
    return [{"name": r.get("name"), "status": r.get("status"), "conclusion": r.get("conclusion"),
             "url": r.get("html_url")} for r in runs]


def run(args: dict, ctx: ToolContext) -> str:
    number = args["number"]
    _check_preconditions(number, ctx)

    _, pr = _gh(ctx, "GET", f"/pulls/{number}")
    if not isinstance(pr, dict):
        raise ToolError("bad_output", f"GitHub returned an unexpected response for PR #{number}. {NOTHING} You may retry once.")
    head = pr.get("head") or {}
    if head.get("ref") != FIX_BRANCH:
        raise ToolError("not_allowed", f"PR #{number} does not come from {FIX_BRANCH}; PatchPilot only merges its own fix. {NOTHING}")
    if pr.get("merged"):
        return json.dumps({"merged": True, "already_merged": True, "number": number,
                           "sha": pr.get("merge_commit_sha"), "ci": None})
    if pr.get("state") != "open":
        raise ToolError("not_allowed", f"PR #{number} is closed without being merged. {NOTHING} Stop; a human must decide.")
    sha = head.get("sha")
    if not sha:
        raise ToolError("bad_output", f"GitHub did not report the head commit of PR #{number}. {NOTHING} You may retry once.")

    status, runs = wait_for_ci(ctx, sha)
    if status == "pending":
        raise ToolError("ci_timeout", f"CI on PR #{number} did not finish within {ctx.ci_timeout_s}s. {NOTHING} "
                                      "Stop; a human can merge it once CI passes.")
    if status == "failed":
        failed = ", ".join(f"{r.get('name')}: {r.get('conclusion')}" for r in runs
                           if r.get("conclusion") not in OK_CONCLUSIONS) or "no successful run"
        raise ToolError("ci_failed", f"CI failed on PR #{number} ({failed}). {NOTHING} Stop; a human must look at the failure.")

    if ctx.approve is None:
        raise ToolError("approval_required", f"merging needs human approval and no approver is connected. {NOTHING}")
    proposal = {"tool": NAME, "repo": ctx.github_repo, "number": number, "title": pr.get("title"),
                "head": FIX_BRANCH, "base": (pr.get("base") or {}).get("ref"), "head_sha": sha,
                "method": "squash", "ci": _ci_summary(runs), "url": pr.get("html_url")}
    if not ctx.approve(proposal):
        raise ToolError("approval_denied", f"the human denied this merge. {NOTHING} Stop the run.")

    status_code, merged = _gh(ctx, "PUT", f"/pulls/{number}/merge",
                              {"merge_method": "squash", "sha": sha}, allow=(405, 409))
    if status_code == 405:
        raise ToolError("not_allowed", f"GitHub says PR #{number} cannot be merged yet (branch protection or conflicts). "
                                       f"{NOTHING} Stop; a human must look at it.")
    if status_code == 409:
        raise ToolError("not_allowed", f"PR #{number} changed after CI ran (head is no longer {sha[:7]}). {NOTHING} Stop.")
    if not isinstance(merged, dict) or not merged.get("merged"):
        raise ToolError("bad_output", f"GitHub answered the merge of PR #{number} unexpectedly; check the PR before retrying.")
    return json.dumps({"merged": True, "already_merged": False, "number": number, "sha": merged.get("sha"),
                       "ci": _ci_summary(runs)})
