"""Shared path checks: every path the model sends is relative to the repo root."""

from __future__ import annotations

from pathlib import Path, PurePosixPath

from agent.tools.errors import ToolError


def resolve_in_repo(repo_root: Path, raw: str, tool: str) -> Path:
    """Return the absolute path for `raw`, or raise ToolError if it leaves the repo."""
    if "\x00" in raw or "\\" in raw:
        raise ToolError("not_allowed", f"path '{raw}' contains invalid characters. Use a plain relative path like 'requirements.txt'.")
    pure = PurePosixPath(raw)
    if pure.is_absolute():
        raise ToolError("not_allowed", f"path '{raw}' is absolute. Use a path relative to the repo root, like 'requirements.txt'.")
    if ".." in pure.parts:
        raise ToolError("not_allowed", f"path '{raw}' goes outside the repo ('..' is not allowed). Use a path relative to the repo root.")
    root = repo_root.resolve()
    target = (root / pure).resolve()
    if target != root and root not in target.parents:
        raise ToolError("not_allowed", f"path '{raw}' resolves outside the repo. Use a path relative to the repo root.")
    return target
