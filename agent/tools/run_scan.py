"""run_scan (read): run Trivy on the sandbox repo and return a compact JSON summary."""

from __future__ import annotations

import json
import subprocess

from agent.tools._paths import resolve_in_repo
from agent.tools.context import ToolContext
from agent.tools.errors import ToolError

NAME = "run_scan"
DESCRIPTION = (
    "Scan the target repo's dependencies for known vulnerabilities with Trivy "
    "(offline, cached DB). Returns JSON: clean, count, and one entry per finding "
    "with id, package, installed, fixed (null when no fixed version exists), severity, title, file."
)
INPUT_SCHEMA = {
    "type": "object",
    "properties": {
        "path": {
            "type": "string",
            "minLength": 1,
            "maxLength": 200,
            "description": "Directory to scan, relative to the repo root. Defaults to '.' (the whole repo).",
        }
    },
    "additionalProperties": False,
}

MAX_TITLE = 200


def build_command(ctx: ToolContext, target) -> list[str]:
    return [
        "trivy", "fs",
        "--cache-dir", str(ctx.trivy_cache),
        "--skip-db-update",
        "--scanners", "vuln",
        "--format", "json",
        "--quiet",
        str(target),
    ]


def summarize(report: dict, path: str) -> dict:
    findings = []
    for result in report.get("Results") or []:
        for vuln in result.get("Vulnerabilities") or []:
            fixed = (vuln.get("FixedVersion") or "").strip() or None
            findings.append(
                {
                    "id": vuln.get("VulnerabilityID"),
                    "package": vuln.get("PkgName"),
                    "installed": vuln.get("InstalledVersion"),
                    "fixed": fixed,
                    "severity": vuln.get("Severity"),
                    "title": (vuln.get("Title") or "")[:MAX_TITLE],
                    "file": result.get("Target"),
                }
            )
    findings.sort(key=lambda f: (f["package"] or "", f["id"] or ""))
    return {"path": path, "clean": not findings, "count": len(findings), "vulnerabilities": findings}


def run(args: dict, ctx: ToolContext) -> str:
    path = args.get("path", ".")
    target = resolve_in_repo(ctx.repo_root, path, NAME)
    if not target.is_dir():
        raise ToolError("not_found", f"directory '{path}' does not exist in the repo. Use '.' to scan the whole repo.")
    try:
        proc = ctx.runner(
            build_command(ctx, target),
            capture_output=True,
            text=True,
            timeout=ctx.scan_timeout_s,
        )
    except FileNotFoundError:
        raise ToolError("tool_down", "the scanner (trivy) is not installed or not on PATH. Scanning is unavailable; stop and report it.") from None
    except subprocess.TimeoutExpired:
        raise ToolError("timeout", f"the scan timed out after {ctx.scan_timeout_s}s. You may retry once; if it times out again, stop.") from None
    if proc.returncode != 0:
        err = (proc.stderr or "").strip().splitlines()
        last = err[-1][:300] if err else "no error output"
        if "skip-db-update" in (proc.stderr or "") or "database" in (proc.stderr or "").lower():
            raise ToolError("tool_down", f"the scanner's vulnerability DB is missing or unreadable ({last}). Run 'make setup' to cache it; scanning is unavailable until then.")
        raise ToolError("tool_down", f"the scanner exited with code {proc.returncode}: {last}")
    try:
        report = json.loads(proc.stdout or "")
    except json.JSONDecodeError:
        raise ToolError("bad_output", "the scanner returned output that is not valid JSON. You may retry once.") from None
    if not isinstance(report, dict):
        raise ToolError("bad_output", "the scanner returned an unexpected JSON shape. You may retry once.")
    return json.dumps(summarize(report, path), ensure_ascii=False)
