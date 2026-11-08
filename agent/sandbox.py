"""The sandbox clone the agent edits: sandbox/work, rebuilt by sandbox/reset.sh."""

from __future__ import annotations

import subprocess
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
RESET_SCRIPT = PROJECT_ROOT / "sandbox" / "reset.sh"
TEMPLATE = PROJECT_ROOT / "sandbox" / "template"
WORK = PROJECT_ROOT / "sandbox" / "work"
MARKER = Path(".git") / "patchpilot-sandbox"
FIX_BRANCH = "patchpilot/fix"


def is_sandbox(path: Path | str) -> bool:
    """True only for a directory created by sandbox/reset.sh."""
    return (Path(path) / MARKER).is_file()


def reset(source: Path | str = TEMPLATE, dest: Path | str = WORK) -> Path:
    """Rebuild the sandbox from `source`. Returns the sandbox path."""
    proc = subprocess.run(
        ["bash", str(RESET_SCRIPT), str(source), str(dest)],
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr.strip() or "sandbox reset failed")
    return Path(dest).resolve()
