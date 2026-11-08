"""Everything a tool needs to know about where it runs."""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

PROJECT_ROOT = Path(__file__).resolve().parents[2]


@dataclass
class ToolContext:
    repo_root: Path
    trivy_cache: Path = PROJECT_ROOT / ".cache" / "trivy"
    test_python: str = str(PROJECT_ROOT / "sandbox" / ".venv" / "bin" / "python")
    scan_timeout_s: int = 120
    test_timeout_s: int = 120
    install_timeout_s: int = 180
    install_deps: bool = True
    runner: Callable = subprocess.run

    def __post_init__(self) -> None:
        self.repo_root = Path(self.repo_root).resolve()
        self.trivy_cache = Path(self.trivy_cache).resolve()
