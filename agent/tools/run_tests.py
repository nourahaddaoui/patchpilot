"""run_tests (read): install the repo's pinned requirements, run pytest, return a JSON summary.

Runs in the sandbox interpreter (sandbox/.venv), never in the agent's own venv,
so a bumped pin is what the tests actually exercise.
"""

from __future__ import annotations

import json
import os
import re
import subprocess

from agent.tools.context import ToolContext
from agent.tools.errors import ToolError

NAME = "run_tests"
DESCRIPTION = (
    "Install the target repo's requirements.txt into the sandbox interpreter and run its "
    "test suite with pytest. Takes no arguments. Returns JSON: green (true only if every "
    "test passed), exit_code, passed, failed, errors, summary, output_tail. Failing tests "
    "are a normal result, not an error."
)
INPUT_SCHEMA = {"type": "object", "properties": {}, "additionalProperties": False}

TAIL_LINES = 30
_COUNT = re.compile(r"(\d+) (passed|failed|errors?)\b")


def _env() -> dict:
    env = dict(os.environ)
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    env["PIP_DISABLE_PIP_VERSION_CHECK"] = "1"
    return env


def _tail(text: str, n: int = TAIL_LINES, width: int = 300) -> str:
    return "\n".join(line[:width] for line in (text or "").strip().splitlines()[-n:])


def _install(ctx: ToolContext) -> None:
    req = ctx.repo_root / "requirements.txt"
    if not ctx.install_deps or not req.is_file():
        return
    try:
        proc = ctx.runner(
            [ctx.test_python, "-m", "pip", "install", "-q", "-r", str(req)],
            cwd=str(ctx.repo_root), capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=ctx.install_timeout_s, env=_env(),
        )
    except FileNotFoundError:
        raise ToolError("tool_down", "the sandbox Python interpreter is missing. Run 'make setup'; tests cannot run until then.") from None
    except subprocess.TimeoutExpired:
        raise ToolError("timeout", f"installing requirements.txt timed out after {ctx.install_timeout_s}s. You may retry once.") from None
    if proc.returncode != 0:
        detail = _tail(proc.stderr or proc.stdout, 5)
        raise ToolError(
            "install_failed",
            f"installing requirements.txt failed, so tests were not run. A pinned version may not exist. pip said: {detail}",
        )
    _install_project(ctx)


def _install_project(ctx: ToolContext) -> None:
    """Real repos often import their own package in tests: install it (no deps), best effort.
    A failure here is not fatal; the tests themselves will show any import error."""
    if not ctx.install_project:
        return
    if not ((ctx.repo_root / "pyproject.toml").is_file() or (ctx.repo_root / "setup.py").is_file()):
        return
    try:
        ctx.runner(
            [ctx.test_python, "-m", "pip", "install", "-q", "--no-deps", "-e", "."],
            cwd=str(ctx.repo_root), capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=ctx.install_timeout_s, env=_env(),
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        pass


def parse_counts(text: str) -> dict:
    counts = {"passed": 0, "failed": 0, "errors": 0}
    summary = ""
    for line in reversed((text or "").strip().splitlines()):
        if _COUNT.search(line) or "no tests ran" in line:
            summary = line.strip("= ").strip()
            for num, kind in _COUNT.findall(line):
                counts["errors" if kind.startswith("error") else kind] = int(num)
            break
    return {**counts, "summary": summary}


def run(args: dict, ctx: ToolContext) -> str:
    _install(ctx)
    try:
        proc = ctx.runner(
            [ctx.test_python, "-m", "pytest", "-q", "-p", "no:cacheprovider"],
            cwd=str(ctx.repo_root), capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=ctx.test_timeout_s, env=_env(),
        )
    except FileNotFoundError:
        raise ToolError("tool_down", "the sandbox Python interpreter is missing. Run 'make setup'; tests cannot run until then.") from None
    except subprocess.TimeoutExpired:
        raise ToolError("timeout", f"the tests timed out after {ctx.test_timeout_s}s. You may retry once; if it times out again, stop.") from None
    output = (proc.stdout or "") + (proc.stderr or "")
    if proc.returncode == 5:  # pytest found nothing: many repos keep unittest tests in tests.py
        fallback = _run_unittest(ctx)
        if fallback is not None:
            return json.dumps(fallback, ensure_ascii=False)
    parsed = parse_counts(output)
    if proc.returncode == 5:
        parsed["summary"] = parsed["summary"] or "no tests collected"
    result = {
        "green": proc.returncode == 0,
        "exit_code": proc.returncode,
        **parsed,
        "output_tail": _tail(output),
    }
    return json.dumps(result, ensure_ascii=False)


_RAN = re.compile(r"Ran (\d+) tests?")
_FAILS = re.compile(r"(failures|errors)=(\d+)")


def _run_unittest(ctx: ToolContext) -> dict | None:
    """`python -m unittest discover` (finds test*.py, e.g. tests.py). None if it finds no tests either."""
    try:
        proc = ctx.runner(
            [ctx.test_python, "-m", "unittest", "discover", "-q"],
            cwd=str(ctx.repo_root), capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=ctx.test_timeout_s, env=_env(),
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return None
    output = (proc.stdout or "") + (proc.stderr or "")
    ran = _RAN.search(output)
    if not ran or int(ran.group(1)) == 0:
        return None
    total = int(ran.group(1))
    bad = {k: int(v) for k, v in _FAILS.findall(output)}
    failed, errors = bad.get("failures", 0), bad.get("errors", 0)
    return {
        "green": proc.returncode == 0,
        "exit_code": proc.returncode,
        "passed": max(total - failed - errors, 0),
        "failed": failed,
        "errors": errors,
        "summary": f"unittest: ran {total}, {failed} failed, {errors} errors",
        "output_tail": _tail(output),
    }
