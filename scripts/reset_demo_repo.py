"""Reset the GitHub demo repo to its vulnerable "baseline" tag. ADMIN ONLY, not part of the agent.

    python scripts/reset_demo_repo.py --dry-run     # print what it would do, change nothing
    python scripts/reset_demo_repo.py               # do it

What it does, on the repo in PATCHPILOT_GITHUB_REPO (environment, else .env):
  1. closes open issues and PRs whose body carries the PatchPilot footer,
  2. deletes branch patchpilot/fix,
  3. saves main's branch protection, removes it, force-updates main to the
     commit of tag "baseline", and restores exactly the saved protection in a
     `finally` block, so protection comes back even if the reset fails half way,
  4. reads the protection back and fails loudly if it differs from what was saved.

Why a separate token: undoing a merge needs a force-push to a protected main, which
needs Administration (write). The agent's GITHUB_TOKEN must never have that, so it
cannot bypass the protection it relies on. The admin token is read ONLY from
.env.admin (git-ignored), never from .env or the environment the agent runs in, and
is never printed.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import Callable

ROOT = Path(__file__).resolve().parents[1]
ADMIN_ENV = ROOT / ".env.admin"
AGENT_ENV = ROOT / ".env"
API = "https://api.github.com"
BRANCH = "main"
FIX_BRANCH = "patchpilot/fix"
TAG = "baseline"
MARK = "PatchPilot (automated dependency remediation)"  # same as agent/tools/create_issue.MARK
BOOL_SETTINGS = ("required_linear_history", "allow_force_pushes", "allow_deletions", "block_creations",
                 "required_conversation_resolution", "lock_branch", "allow_fork_syncing")


class ResetError(Exception):
    pass


# --- config -------------------------------------------------------------------------

def read_env_file(path: Path) -> dict[str, str]:
    """KEY=VALUE lines of a dotenv file; nothing is exported to os.environ."""
    out: dict[str, str] = {}
    if not path.is_file():
        return out
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, value = line.split("=", 1)
            out[key.strip()] = value.strip().strip('"').strip("'")
    return out


def admin_token(path: Path = ADMIN_ENV) -> str:
    token = read_env_file(path).get("PATCHPILOT_ADMIN_TOKEN", "")
    if not token:
        raise ResetError(f"PATCHPILOT_ADMIN_TOKEN is not set in {path.name} (it is read from there only).")
    return token


def target_repo(env_path: Path = AGENT_ENV) -> str:
    repo = os.environ.get("PATCHPILOT_GITHUB_REPO") or read_env_file(env_path).get("PATCHPILOT_GITHUB_REPO", "")
    if repo.count("/") != 1:
        raise ResetError("PATCHPILOT_GITHUB_REPO must be set to 'owner/name'.")
    return repo


# --- HTTP ---------------------------------------------------------------------------

def urllib_http(method: str, url: str, headers: dict, body, timeout: int):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read()
            return resp.status, json.loads(raw) if raw else None
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read() or b"null")
        except json.JSONDecodeError:
            return e.code, None


class GitHub:
    """Tiny REST client. In dry-run mode every write is printed and skipped; reads still run."""

    def __init__(self, repo: str, token: str, http: Callable = urllib_http, dry_run: bool = False,
                 out: Callable[[str], None] = print):
        self.repo, self._token, self.http, self.dry_run, self.out = repo, token, http, dry_run, out

    def call(self, method: str, path: str, body=None, allow: tuple[int, ...] = ()):
        if method != "GET" and self.dry_run:
            self.out(f"  [dry-run] would {method} {path}")
            return None, None
        headers = {"Accept": "application/vnd.github+json", "Authorization": f"Bearer {self._token}",
                   "X-GitHub-Api-Version": "2022-11-28", "User-Agent": "patchpilot-reset"}
        try:
            status, payload = self.http(method, f"{API}/repos/{self.repo}{path}", headers, body, 30)
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            raise ResetError(f"{method} {path}: GitHub could not be reached ({type(e).__name__})") from None
        if status >= 400 and status not in allow:
            message = payload.get("message") if isinstance(payload, dict) else ""
            raise ResetError(f"{method} {path} failed: HTTP {status} {message}".strip())
        return status, payload


# --- protection ---------------------------------------------------------------------

def _logins(items) -> list[str]:
    return [i.get("login") for i in items or [] if isinstance(i, dict) and i.get("login")]


def _slugs(items) -> list[str]:
    return [i.get("slug") for i in items or [] if isinstance(i, dict) and i.get("slug")]


def _actors(block) -> dict | None:
    if not isinstance(block, dict):
        return None
    return {"users": _logins(block.get("users")), "teams": _slugs(block.get("teams")), "apps": _slugs(block.get("apps"))}


def protection_to_put(saved: dict) -> dict:
    """Turn GET /branches/{b}/protection into the body PUT expects (same settings)."""
    checks = saved.get("required_status_checks")
    reviews = saved.get("required_pull_request_reviews")
    body: dict = {
        "required_status_checks": None if not checks else {
            "strict": bool(checks.get("strict")),
            "checks": [{"context": c["context"], **({"app_id": c["app_id"]} if c.get("app_id") is not None else {})}
                       for c in checks.get("checks") or []]
            or [{"context": c} for c in checks.get("contexts") or []],
        },
        "enforce_admins": bool((saved.get("enforce_admins") or {}).get("enabled")),
        "required_pull_request_reviews": None if not reviews else {
            k: v for k, v in {
                "dismiss_stale_reviews": bool(reviews.get("dismiss_stale_reviews")),
                "require_code_owner_reviews": bool(reviews.get("require_code_owner_reviews")),
                "required_approving_review_count": reviews.get("required_approving_review_count", 0),
                "require_last_push_approval": bool(reviews.get("require_last_push_approval")),
                "dismissal_restrictions": _actors(reviews.get("dismissal_restrictions")),
                "bypass_pull_request_allowances": _actors(reviews.get("bypass_pull_request_allowances")),
            }.items() if v is not None
        },
        "restrictions": _actors(saved.get("restrictions")),
    }
    for key in BOOL_SETTINGS:
        if isinstance(saved.get(key), dict):
            body[key] = bool(saved[key].get("enabled"))
    return body


def restore_protection(gh: GitHub, saved: dict) -> None:
    gh.call("PUT", f"/branches/{BRANCH}/protection", protection_to_put(saved))
    if (saved.get("required_signatures") or {}).get("enabled"):
        gh.call("POST", f"/branches/{BRANCH}/protection/required_signatures")
    if gh.dry_run:
        return
    _, now = gh.call("GET", f"/branches/{BRANCH}/protection")
    if protection_to_put(now or {}) != protection_to_put(saved):
        raise ResetError("protection was restored but reads back DIFFERENT from what was saved; check it by hand now.")


# --- steps --------------------------------------------------------------------------

def baseline_sha(gh: GitHub) -> str:
    _, ref = gh.call("GET", f"/git/ref/tags/{TAG}")
    obj = (ref or {}).get("object") or {}
    if obj.get("type") == "tag":  # annotated tag: follow it to the commit
        _, tag = gh.call("GET", f"/git/tags/{obj.get('sha')}")
        obj = (tag or {}).get("object") or {}
    if obj.get("type") != "commit" or not obj.get("sha"):
        raise ResetError(f"tag '{TAG}' does not point to a commit.")
    return obj["sha"]


def close_patchpilot_items(gh: GitHub) -> None:
    _, issues = gh.call("GET", "/issues?state=open&per_page=100")  # includes PRs
    for item in issues or []:
        if MARK not in (item.get("body") or ""):
            continue
        kind, path = ("PR", f"/pulls/{item['number']}") if "pull_request" in item else ("issue", f"/issues/{item['number']}")
        gh.out(f"close {kind} #{item['number']}: {item.get('title', '')[:80]}")
        gh.call("PATCH", path, {"state": "closed"})


def delete_fix_branch(gh: GitHub) -> None:
    status, _ = gh.call("GET", f"/git/ref/heads/{FIX_BRANCH}", allow=(404,))
    if status == 404:
        gh.out(f"branch {FIX_BRANCH}: not there")
        return
    gh.out(f"delete branch {FIX_BRANCH}")
    gh.call("DELETE", f"/git/refs/heads/{FIX_BRANCH}")


def reset_main(gh: GitHub) -> None:
    sha = baseline_sha(gh)  # resolve before touching protection
    _, head = gh.call("GET", f"/git/ref/heads/{BRANCH}")
    current = ((head or {}).get("object") or {}).get("sha")
    if current == sha:
        gh.out(f"{BRANCH} is already at {TAG} ({sha[:7]})")
        return
    status, saved = gh.call("GET", f"/branches/{BRANCH}/protection", allow=(404,))
    protected = status != 404
    gh.out(f"{BRANCH}: {str(current)[:7]} -> {TAG} {sha[:7]}" + (" (protection removed, then restored)" if protected else ""))
    if protected:
        gh.call("DELETE", f"/branches/{BRANCH}/protection")
    try:
        gh.call("PATCH", f"/git/refs/heads/{BRANCH}", {"sha": sha, "force": True})
    finally:
        if protected:
            restore_protection(gh, saved)
            gh.out(f"{BRANCH} protection restored")


def reset(gh: GitHub) -> None:
    gh.out(f"Resetting {gh.repo}{' (dry run: no changes)' if gh.dry_run else ''}")
    close_patchpilot_items(gh)
    delete_fix_branch(gh)
    reset_main(gh)
    gh.out("done")


def main(argv: list[str] | None = None, http: Callable = urllib_http,
         admin_env: Path = ADMIN_ENV, agent_env: Path = AGENT_ENV) -> int:
    parser = argparse.ArgumentParser(description="Reset the PatchPilot demo repo to its 'baseline' tag (admin only).")
    parser.add_argument("--dry-run", action="store_true", help="print what would happen, change nothing")
    ns = parser.parse_args(argv)
    try:
        gh = GitHub(target_repo(agent_env), admin_token(admin_env), http=http, dry_run=ns.dry_run)
        reset(gh)
    except ResetError as e:
        print(f"reset failed: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
