"""Scan the repo for credentials before it is frozen or pushed.

    python scripts/scan_secrets.py          # scans every file git tracks; exit 1 if anything is found
    python scripts/scan_secrets.py PATH...  # scans these files or folders instead

Looks for real-looking tokens and keys (GitHub, OpenAI, LangSmith, AWS, Slack,
private keys, hard-coded secret assignments) and for committed .env files.
Findings are printed masked, never in full, so the scan's own output cannot leak them.

Obvious test values are not findings: a value containing a placeholder word
(example, fake, dummy, test, placeholder, xxxx, ...) or a line marked
`scan-secrets: allow`.
"""

from __future__ import annotations

import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

PATTERNS = {
    "GitHub token": re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9]{36,}|github_pat_[A-Za-z0-9_]{60,})\b"),
    "OpenAI key": re.compile(r"\bsk-(?:proj-|svcacct-|admin-)?[A-Za-z0-9_-]{32,}\b"),
    "LangSmith key": re.compile(r"\blsv2_(?:pt|sk)_[A-Za-z0-9_]{24,}\b"),
    "AWS access key": re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b"),
    "Slack token": re.compile(r"\bxox[abposr]-[A-Za-z0-9-]{10,}\b"),
    "Private key": re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH |DSA |PGP )?PRIVATE KEY( BLOCK)?-----"),
    "Hard-coded secret": re.compile(
        r"""(?i)\b[\w-]*(?:api[_-]?key|token|secret|passw(?:or)?d)\b\s*[:=]\s*["']([^"'\s]{16,})["']"""),
}
PLACEHOLDER = re.compile(r"(?i)example|fake|dummy|placeholder|changeme|redacted|xxxx|test|sample|your[_-]|<|\*\*\*|\.\.\.")
ALLOW_MARK = "scan-secrets: allow"
SKIP_DIRS = {".git", ".venv", ".venvs", "venv", "__pycache__", ".cache", "node_modules", ".pytest_cache"}
SKIP_SUFFIXES = {".png", ".jpg", ".jpeg", ".gif", ".pdf", ".zip", ".gz", ".whl", ".pyc", ".ico"}
ENV_FILE = re.compile(r"^\.env(\..+)?$")
ENV_OK = {".env.example", ".env.sample", ".env.template"}


@dataclass(frozen=True)
class Finding:
    path: str
    line: int
    kind: str
    masked: str

    def __str__(self) -> str:
        return f"{self.path}:{self.line}: {self.kind}: {self.masked}"


def mask(value: str) -> str:
    return value[:4] + "…" + f"({len(value)} chars)" if len(value) > 8 else "…"


def scan_text(text: str, path: str = "<text>") -> list[Finding]:
    found = []
    for n, line in enumerate(text.splitlines(), 1):
        if ALLOW_MARK in line:
            continue
        for kind, pattern in PATTERNS.items():
            for m in pattern.finditer(line):
                value = m.group(1) if m.groups() and m.group(1) else m.group(0)
                if kind != "Private key" and PLACEHOLDER.search(value):
                    continue
                found.append(Finding(path, n, kind, mask(value)))
    return found


def tracked_files(root: Path) -> list[Path]:
    """Files git tracks (what would be pushed); every file under root if git is unavailable."""
    try:
        out = subprocess.run(["git", "ls-files", "-z"], cwd=root, capture_output=True, check=True).stdout
        return [root / p for p in out.decode("utf-8", "replace").split("\0") if p]
    except (OSError, subprocess.CalledProcessError):
        return [p for p in root.rglob("*") if p.is_file() and not SKIP_DIRS.intersection(p.relative_to(root).parts)]


def scan_paths(paths: list[Path], root: Path = ROOT) -> list[Finding]:
    found = []
    for p in paths:
        if p.is_dir():
            found += scan_paths([f for f in p.rglob("*") if f.is_file()
                                 and not SKIP_DIRS.intersection(f.relative_to(p).parts)], root)
            continue
        if not p.is_file() or p.suffix.lower() in SKIP_SUFFIXES:
            continue
        rel = str(p.relative_to(root)) if root in p.resolve().parents else str(p)
        if ENV_FILE.match(p.name) and p.name not in ENV_OK:
            found.append(Finding(rel, 0, "Committed .env file", "the whole file"))
        try:
            found += scan_text(p.read_text(encoding="utf-8"), rel)
        except (UnicodeDecodeError, OSError):
            continue
    return found


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    paths = [Path(a).resolve() for a in args] if args else tracked_files(ROOT)
    findings = scan_paths(paths)
    for f in findings:
        print(f)
    print(f"\n{len(findings)} possible credential(s) in {len(paths)} path(s)." if findings
          else f"No credentials found in {len(paths)} path(s).")
    return 1 if findings else 0


if __name__ == "__main__":
    sys.exit(main())
