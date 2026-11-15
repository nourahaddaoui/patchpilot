"""Everything a tool needs to know about where it runs."""

from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional

PROJECT_ROOT = Path(__file__).resolve().parents[2]
_VENV_BIN = ("Scripts", "python.exe") if os.name == "nt" else ("bin", "python")
SANDBOX_PYTHON = PROJECT_ROOT / "sandbox" / ".venv" / _VENV_BIN[0] / _VENV_BIN[1]


def default_trivy_cache() -> Path:
    """$PATCHPILOT_TRIVY_CACHE if set (e.g. a cache on another disk), else .cache/trivy."""
    return Path(os.environ.get("PATCHPILOT_TRIVY_CACHE") or PROJECT_ROOT / ".cache" / "trivy")


@dataclass
class ToolContext:
    repo_root: Path
    trivy_cache: Path = field(default_factory=default_trivy_cache)
    test_python: str = str(SANDBOX_PYTHON)
    scan_timeout_s: int = 120
    test_timeout_s: int = 120
    install_timeout_s: int = 180
    install_deps: bool = True
    # Real repos: also `pip install --no-deps -e .` so tests can import the project itself.
    install_project: bool = False
    runner: Callable = subprocess.run
    # Human checkpoint for write tools: called with the proposal (incl. full diff),
    # returns True to approve. None means no approver, so every write is refused.
    approve: Optional[Callable[[dict], bool]] = None
    # create_issue: "local" (default, free, offline) writes to the sandbox's .git dir;
    # "github" posts to github_repo ("owner/name") with $GITHUB_TOKEN.
    issue_mode: str = field(default_factory=lambda: os.environ.get("PATCHPILOT_ISSUES", "local"))
    github_repo: Optional[str] = field(default_factory=lambda: os.environ.get("PATCHPILOT_GITHUB_REPO") or None)
    github_timeout_s: int = 20
    # HTTP transport for create_issue (tests inject a fake): (method, url, headers, body, timeout) -> (status, json)
    http: Optional[Callable] = None
    # Permanent copy of every filed issue (survives sandbox resets). The CLI sets logs/issues.
    issue_archive: Optional[Path] = None

    def __post_init__(self) -> None:
        self.repo_root = Path(self.repo_root).resolve()
        self.trivy_cache = Path(self.trivy_cache).resolve()
