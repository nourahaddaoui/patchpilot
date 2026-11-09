"""The sandbox clone the agent edits: sandbox/work, rebuilt by sandbox/reset.sh."""

from __future__ import annotations

import os
import shutil
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


def bash() -> str:
    """Path to a POSIX bash. On Windows, prefer Git Bash over System32\\bash.exe (WSL)."""
    if os.name == "nt":
        git = shutil.which("git")
        if git:
            git_root = Path(git).resolve().parents[1]  # ...\Git\cmd\git.exe -> ...\Git
            for candidate in (git_root / "bin" / "bash.exe", git_root / "usr" / "bin" / "bash.exe"):
                if candidate.is_file():
                    return str(candidate)
    return shutil.which("bash") or "bash"


_GIT = ["git", "-c", "user.name=PatchPilot", "-c", "user.email=patchpilot@localhost",
        "-c", "commit.gpgsign=false"]


def commit_fix(repo: Path | str, message: str) -> str | None:
    """Commit requirements.txt on the sandbox's fix branch. Returns the short sha, or None if nothing changed.

    Refuses to touch anything that is not a sandbox made by reset.sh.
    """
    repo = Path(repo)
    if not is_sandbox(repo):
        raise RuntimeError(f"'{repo}' is not a PatchPilot sandbox; refusing to commit")
    def git(*args: str) -> subprocess.CompletedProcess:
        return subprocess.run([*_GIT, *args], cwd=repo, capture_output=True, text=True)
    branch = git("rev-parse", "--abbrev-ref", "HEAD").stdout.strip()
    if branch != FIX_BRANCH:
        raise RuntimeError(f"sandbox is on '{branch}', expected '{FIX_BRANCH}'; refusing to commit")
    if not git("status", "--porcelain", "--", "requirements.txt").stdout.strip():
        return None
    git("add", "--", "requirements.txt")
    proc = git("commit", "-q", "-m", message)
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr.strip() or "git commit failed")
    return git("rev-parse", "--short", "HEAD").stdout.strip()


def reset(source: Path | str = TEMPLATE, dest: Path | str = WORK) -> Path:
    """Rebuild the sandbox from `source`. Returns the sandbox path."""
    proc = subprocess.run(
        [bash(), str(RESET_SCRIPT), str(source), str(dest)],
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr.strip() or "sandbox reset failed")
    return Path(dest).resolve()
