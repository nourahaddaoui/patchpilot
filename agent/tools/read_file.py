"""read_file (read): return the text of an allowlisted file in the sandbox repo."""

from __future__ import annotations

from fnmatch import fnmatch

from agent.tools._paths import resolve_in_repo
from agent.tools.context import ToolContext
from agent.tools.errors import ToolError

NAME = "read_file"
DESCRIPTION = (
    "Read a text file from the target repo. Only these files can be read: "
    "requirements*.txt, *.py, README.md, pyproject.toml, setup.cfg. "
    "Hidden files and folders (.git, .env, .github) are never readable."
)
INPUT_SCHEMA = {
    "type": "object",
    "properties": {
        "path": {
            "type": "string",
            "minLength": 1,
            "maxLength": 200,
            "description": "Path relative to the repo root, e.g. 'requirements.txt' or 'tests/test_client.py'.",
        }
    },
    "required": ["path"],
    "additionalProperties": False,
}

ALLOWED_NAMES = ("requirements*.txt", "*.py", "README.md", "pyproject.toml", "setup.cfg")
MAX_BYTES = 100_000


def run(args: dict, ctx: ToolContext) -> str:
    raw = args["path"]
    target = resolve_in_repo(ctx.repo_root, raw, NAME)
    rel_parts = target.relative_to(ctx.repo_root).parts
    if any(part.startswith(".") for part in rel_parts):
        raise ToolError("not_allowed", f"path '{raw}' is a hidden file or folder and is never readable.")
    if not any(fnmatch(target.name, pattern) for pattern in ALLOWED_NAMES):
        raise ToolError(
            "not_allowed",
            f"path '{raw}' is not on the read allowlist. Allowed: {', '.join(ALLOWED_NAMES)}.",
        )
    if not target.is_file():
        raise ToolError("not_found", f"file '{raw}' does not exist. Paths are relative to the repo root, e.g. 'requirements.txt'.")
    size = target.stat().st_size
    if size > MAX_BYTES:
        raise ToolError("too_large", f"file '{raw}' is {size} bytes; the limit is {MAX_BYTES}.")
    try:
        return target.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        raise ToolError("bad_output", f"file '{raw}' is not UTF-8 text.") from None
