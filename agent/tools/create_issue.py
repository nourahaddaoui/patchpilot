"""create_issue (write): file an issue about a vulnerability the agent cannot (or should not) fix alone.

Safety rules:
- Nothing is filed until ctx.approve(proposal) returns True. The proposal carries
  the exact title and body that will be posted.
- Idempotent: an issue whose title matches one already filed (local store, and
  open issues on GitHub in github mode) is not filed again; the existing one is returned.
- @mentions in the text are put in code spans so a filed issue never pings anyone.
- Costs nothing by default: mode "local" writes the issue to a JSON file inside the
  sandbox's .git directory (never committed, wiped by reset). Mode "github" posts to
  ctx.github_repo with the token in $GITHUB_TOKEN (free API). The token is read at
  call time and never logged or returned.
"""

from __future__ import annotations

import json
import os
import re
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

from agent.sandbox import is_sandbox
from agent.tools.context import ToolContext
from agent.tools.errors import ToolError

NAME = "create_issue"
DESCRIPTION = (
    "File an issue for a human to follow up, e.g. a vulnerability with no fixed version, a fix "
    "that keeps breaking the tests, or anything you must not change yourself (CI files, code). "
    "A human must approve the exact title and body first; if they deny it, nothing is filed and "
    "the run must stop. Filing the same title twice returns the existing issue. "
    "Returns JSON: filed, existing, number, url, mode."
)
INPUT_SCHEMA = {
    "type": "object",
    "properties": {
        "title": {
            "type": "string",
            "minLength": 5,
            "maxLength": 120,
            "pattern": r"[^\r\n]+",
            "description": "One-line title, e.g. 'CVE-2024-35195: no fixed version of requests'.",
        },
        "body": {
            "type": "string",
            "minLength": 1,
            "maxLength": 4000,
            "description": "Markdown body: what was found, what was tried, and what a human should decide.",
        },
    },
    "required": ["title", "body"],
    "additionalProperties": False,
}

STORE = Path(".git") / "patchpilot-issues"
FOOTER = "\n\n---\nFiled by PatchPilot (automated dependency remediation) after human approval."
API = "https://api.github.com"
_MENTION = re.compile(r"(?<![\w`])@([A-Za-z0-9](?:[A-Za-z0-9-]{0,38}))")
_REPO = re.compile(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+")


def neutralize(text: str) -> str:
    """Wrap @mentions in code spans so GitHub does not notify anyone."""
    return _MENTION.sub(r"`@\1`", text)


def title_key(title: str) -> str:
    return " ".join(title.split()).casefold()


# --- local store -----------------------------------------------------------------

def _store(ctx: ToolContext) -> Path:
    return ctx.repo_root / STORE


def _local_issues(ctx: ToolContext) -> list[dict]:
    d = _store(ctx)
    if not d.is_dir():
        return []
    issues = []
    for p in sorted(d.glob("*.json")):
        try:
            issues.append(json.loads(p.read_text(encoding="utf-8")))
        except (OSError, json.JSONDecodeError):
            continue
    return issues


def _save_local(ctx: ToolContext, record: dict) -> tuple[dict, str | None]:
    """Save to the sandbox store (idempotency for this run) and, if configured, a
    permanent copy in ctx.issue_archive that survives sandbox resets."""
    d = _store(ctx)
    d.mkdir(parents=True, exist_ok=True)
    number = len(list(d.glob("*.json"))) + 1
    record = {**record, "number": record.get("number") or number}
    text = json.dumps(record, ensure_ascii=False, indent=2)
    (d / f"{number:04d}.json").write_text(text, encoding="utf-8")
    saved_to = None
    if ctx.issue_archive is not None:
        archive = Path(ctx.issue_archive)
        archive.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        path = archive / f"{stamp}-{number:04d}.json"
        path.write_text(text, encoding="utf-8")
        saved_to = str(path)
    return record, saved_to


# --- GitHub ----------------------------------------------------------------------

def _urllib_http(method: str, url: str, headers: dict, body: dict | None, timeout: int):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, json.loads(resp.read() or b"null")
    except urllib.error.HTTPError as e:
        try:
            payload = json.loads(e.read() or b"null")
        except json.JSONDecodeError:
            payload = None
        return e.code, payload


def _github(ctx: ToolContext, method: str, path: str, body: dict | None = None):
    token = os.environ.get("GITHUB_TOKEN", "").strip()
    if not token:
        raise ToolError("tool_down", "github mode needs a GITHUB_TOKEN environment variable and none is set. Nothing was filed.")
    headers = {
        "Accept": "application/vnd.github+json",
        "Authorization": f"Bearer {token}",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "patchpilot-agent",
    }
    if body is not None:
        headers["Content-Type"] = "application/json"
    http = ctx.http or _urllib_http
    try:
        status, payload = http(method, f"{API}{path}", headers, body, ctx.github_timeout_s)
    except (urllib.error.URLError, TimeoutError, OSError):
        raise ToolError("tool_down", "GitHub could not be reached. Nothing was filed; you may retry once.") from None
    if status in (401, 403):
        raise ToolError("not_allowed", f"GitHub refused the request (HTTP {status}): the token cannot file issues on {ctx.github_repo}. Nothing was filed.")
    if status == 404:
        raise ToolError("not_found", f"GitHub repo '{ctx.github_repo}' was not found or issues are disabled. Nothing was filed.")
    if status >= 400:
        raise ToolError("tool_down", f"GitHub returned HTTP {status}. Nothing was filed; you may retry once.")
    return payload


def _github_open_issues(ctx: ToolContext) -> list[dict]:
    payload = _github(ctx, "GET", f"/repos/{ctx.github_repo}/issues?state=open&per_page=100")
    if not isinstance(payload, list):
        raise ToolError("bad_output", "GitHub returned an unexpected response when listing issues. You may retry once.")
    return [
        {"title": i.get("title", ""), "number": i.get("number"), "url": i.get("html_url")}
        for i in payload
        if isinstance(i, dict) and "pull_request" not in i
    ]


# --- tool ------------------------------------------------------------------------

def run(args: dict, ctx: ToolContext) -> str:
    title = " ".join(args["title"].split())
    body = neutralize(args["body"].strip()) + FOOTER
    title_safe = neutralize(title)
    mode = ctx.issue_mode

    if mode not in ("local", "github"):
        raise ToolError("not_allowed", f"issue mode '{mode}' is not supported (use 'local' or 'github'). Nothing was filed.")
    if not is_sandbox(ctx.repo_root):
        raise ToolError("not_allowed", "the target repo is not a sandbox clone; issues can only be filed for sandbox/work. Nothing was filed.")
    if mode == "github" and not (ctx.github_repo and _REPO.fullmatch(ctx.github_repo)):
        raise ToolError("not_allowed", "github mode needs PATCHPILOT_GITHUB_REPO set to 'owner/name'. Nothing was filed.")

    key = title_key(title_safe)
    known = _local_issues(ctx)
    if mode == "github":
        known = known + _github_open_issues(ctx)
    for issue in known:
        if title_key(issue.get("title", "")) == key:
            return json.dumps(
                {"filed": False, "existing": True, "number": issue.get("number"), "url": issue.get("url"),
                 "mode": issue.get("mode", mode), "title": issue.get("title")},
                ensure_ascii=False,
            )

    if ctx.approve is None:
        raise ToolError("approval_required", "filing an issue needs human approval and no approver is connected. Nothing was filed.")
    proposal = {"tool": NAME, "mode": mode, "repo": ctx.github_repo if mode == "github" else None,
                "title": title_safe, "body": body}
    if not ctx.approve(proposal):
        raise ToolError("approval_denied", "the human denied this issue. Nothing was filed. Stop the run.")

    record = {"title": title_safe, "body": body, "mode": mode,
              "created_at": datetime.now(timezone.utc).isoformat(), "url": None, "number": None}
    if mode == "github":
        created = _github(ctx, "POST", f"/repos/{ctx.github_repo}/issues", {"title": title_safe, "body": body})
        if not isinstance(created, dict) or "number" not in created:
            raise ToolError("bad_output", "GitHub accepted the request but returned an unexpected response; check the repo before retrying.")
        record.update(number=created["number"], url=created.get("html_url"))
    record, saved_to = _save_local(ctx, record)
    result = {"filed": True, "existing": False, "number": record["number"], "url": record["url"],
              "mode": mode, "title": title_safe}
    if saved_to:
        result["saved_to"] = saved_to
    return json.dumps(result, ensure_ascii=False)
