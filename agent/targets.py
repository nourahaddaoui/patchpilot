"""Real repositories as targets.

    python -m agent --target https://github.com/owner/repo
    python -m agent --target C:/code/some-repo

PatchPilot never edits the real repository. A target is cloned (git clone, so
only tracked files come along) into sandbox/sources/<name>, then sandbox/reset.sh
copies it into sandbox/work, the throwaway sandbox the agent edits. The fix lands
as a commit on branch patchpilot/fix inside sandbox/work; pushing it anywhere is
left to a human.

Each target gets its own test interpreter (sandbox/.venvs/<name>), so one repo's
dependencies never leak into another repo's test run.
"""

from __future__ import annotations

import os
import re
import shutil
import stat
import subprocess
import sys
from pathlib import Path

from agent.sandbox import PROJECT_ROOT, TEMPLATE

SOURCES = PROJECT_ROOT / "sandbox" / "sources"
VENVS = PROJECT_ROOT / "sandbox" / ".venvs"
_URL = re.compile(r"^(https?://|git@|ssh://|git://)")
_SLUG = re.compile(r"[^A-Za-z0-9._-]+")


class TargetError(Exception):
    pass


def is_url(target: str) -> bool:
    return bool(_URL.match(target))


def slug(target: str) -> str:
    name = target.rstrip("/\\")
    if name.endswith(".git"):
        name = name[:-4]
    parts = re.split(r"[/\\:]", name)
    tail = "-".join(p for p in parts[-2:] if p) if is_url(target) else parts[-1]
    return _SLUG.sub("-", tail).strip("-") or "target"


_GITHUB = re.compile(r"^(?:https?://(?:www\.)?github\.com/|git@github\.com:|ssh://git@github\.com/)"
                     r"([A-Za-z0-9-]+)/([A-Za-z0-9._-]+?)(?:\.git)?/?$")


def github_repo(target: str | None) -> str | None:
    """'owner/name' for a GitHub URL target, else None. Used to file issues on the repo being fixed."""
    m = _GITHUB.match((target or "").strip())
    return f"{m.group(1)}/{m.group(2)}" if m else None


def _rmtree(path: Path) -> None:
    def onerror(func, p, _exc):  # git objects are read-only on Windows
        os.chmod(p, stat.S_IWRITE)
        func(p)
    shutil.rmtree(path, onerror=onerror)


def fetch(target: str, sources: Path = SOURCES, timeout_s: int = 300) -> Path:
    """Clone a URL or a local git repo into sources/<slug> (fresh each time).
    A local folder that is not a git repo is used as it is (reset.sh copies it)."""
    if not is_url(target):
        path = Path(target).expanduser().resolve()
        if not path.is_dir():
            raise TargetError(f"'{target}' is not a URL or an existing folder.")
        if path == TEMPLATE.resolve():
            return path
        if not (path / ".git").exists():
            return path
    dest = sources / slug(target)
    sources.mkdir(parents=True, exist_ok=True)
    if dest.exists():
        if dest.resolve().parent != sources.resolve():
            raise TargetError(f"refusing to delete '{dest}': not inside {sources}")
        _rmtree(dest)
    cmd = ["git", "clone", "--quiet"]
    if is_url(target):
        cmd += ["--depth", "1"]
    cmd += [target if is_url(target) else str(Path(target).resolve()), str(dest)]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=timeout_s,
                              env={**os.environ, "GIT_TERMINAL_PROMPT": "0"})
    except subprocess.TimeoutExpired:
        raise TargetError(f"cloning '{target}' timed out after {timeout_s}s.") from None
    if proc.returncode != 0:
        lines = (proc.stderr or "").strip().splitlines()
        raise TargetError(f"could not clone '{target}': {lines[-1] if lines else 'unknown error'}")
    return dest


def target_python(name: str, venvs: Path = VENVS, base_python: str = sys.executable) -> str:
    """A per-target virtualenv with pytest, created on first use."""
    venv = venvs / name
    py = venv / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    if not py.is_file():
        venvs.mkdir(parents=True, exist_ok=True)
        for cmd in ([base_python, "-m", "venv", str(venv)],
                    [str(py), "-m", "pip", "install", "-q", "pytest==8.3.3"]):
            proc = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace")
            if proc.returncode != 0:
                raise TargetError(f"could not prepare the test interpreter: {(proc.stderr or proc.stdout).strip()[-300:]}")
    return str(py)
