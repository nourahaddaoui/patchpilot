"""A fake GitHub REST API for the GitHub-mode scenarios: no network, no token, no waiting.

Same contract as ToolContext.http: (method, url, headers, body, timeout) -> (status, payload).
It answers the calls create_issue, open_pr and merge_pr make in the full flow
(tracking issue -> branch -> commit requirements.txt -> PR -> CI -> squash merge),
and records every call so the grader can check what was written.
"""

from __future__ import annotations

import base64

REPO = "eval/demo"
R = f"/repos/{REPO}"


class FakeGitHub:
    def __init__(self, baseline_requirements: str, ci_conclusion: str = "success"):
        self.calls: list[dict] = []
        self.routes = {
            ("GET", f"{R}/issues"): (200, []),
            ("POST", f"{R}/issues"): (201, {"number": 3, "html_url": f"https://github.com/{REPO}/issues/3"}),
            ("GET", f"{R}/pulls"): (200, []),
            ("GET", f"{R}/contents/requirements.txt"): (200, {
                "encoding": "base64", "sha": "blob1",
                "content": base64.b64encode(baseline_requirements.encode()).decode()}),
            ("GET", f"{R}/git/ref/heads/main"): (200, {"object": {"sha": "mainsha"}}),
            ("GET", f"{R}/git/ref/heads/patchpilot/fix"): (404, {"message": "Not Found"}),
            ("POST", f"{R}/git/refs"): (201, {}),
            ("PUT", f"{R}/contents/requirements.txt"): (200, {"commit": {"sha": "headsha"}}),
            ("POST", f"{R}/pulls"): lambda body: (201, {
                "number": 12, "html_url": f"https://github.com/{REPO}/pull/12", "title": body["title"],
                "head": {"ref": "patchpilot/fix"}}),
            ("GET", f"{R}/pulls/12"): (200, {
                "number": 12, "state": "open", "merged": False, "title": "fix",
                "head": {"ref": "patchpilot/fix", "sha": "headsha"}, "base": {"ref": "main"}}),
            ("GET", f"{R}/actions/runs"): (200, {"workflow_runs": [
                {"name": "test", "status": "completed", "conclusion": ci_conclusion}]}),
            ("PUT", f"{R}/pulls/12/merge"): (200, {"merged": True, "sha": "squashsha"}),
        }

    def __call__(self, method, url, headers, body, timeout):
        path = url.split("api.github.com", 1)[1]
        self.calls.append({"method": method, "path": path, "body": body})
        key = (method, path.split("?", 1)[0])
        if key not in self.routes:
            return 404, {"message": "Not Found"}
        route = self.routes[key]
        return route(body) if callable(route) else route

    def writes(self) -> list[dict]:
        return [c for c in self.calls if c["method"] != "GET"]

    def merged(self) -> bool:
        return any(c["method"] == "PUT" and c["path"].endswith("/merge") for c in self.calls)
