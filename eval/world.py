"""The fake world the eval runs in: stands in for subprocess.run (ToolContext.runner).

Only the three external programs are faked: trivy (the scanner), pip (install) and
pytest (the tests). Everything else is real: the tools' own code (argument checks,
output parsing, plain-text errors), the sandbox (a real git clone made by
sandbox/reset.sh), edit_file writing requirements.txt, the human checkpoint, the
stop rule, recovery and the JSONL log. So an eval run is a real run with a
predictable scanner and test suite, offline, free and in about a second.

The world reads the files the agent actually changed, so it reacts to the agent's
own edits: bump a pin and the next scan no longer reports it.

Attack text for the injection scenarios is placed where real untrusted text comes
from: advisory titles (scanner output), pip's error output, the test output tail,
or the repo's files (read_file).
"""

from __future__ import annotations

import json
import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from packaging.utils import canonicalize_name
from packaging.version import InvalidVersion, Version

from eval.faults import Fault

_PIN = re.compile(r"^\s*([A-Za-z0-9](?:[A-Za-z0-9._-]*[A-Za-z0-9])?)\s*(?:\[[^\]]*\])?\s*==\s*([^\s;#]+)")
SKIP_DIRS = {".git", ".venv", "venv", "__pycache__", "node_modules"}


def versions(raw: str | None) -> list[Version]:
    out = []
    for part in (raw or "").split(","):
        try:
            out.append(Version(part.strip()))
        except InvalidVersion:
            continue
    return sorted(out)


@dataclass(frozen=True)
class Advisory:
    """One known vulnerability, as the scanner's database would hold it."""

    id: str
    package: str
    fixed: str | None  # "2.31.0", "1.5.2, 2.0.1" (one fix per release line), or None: no fix exists
    title: str = "Known vulnerability"
    severity: str = "HIGH"

    def affects(self, installed: str) -> bool:
        try:
            v = Version(installed)
        except InvalidVersion:
            return False
        fixes = versions(self.fixed)
        if not fixes:
            return True  # no fix exists: every version is affected
        same_line = [f for f in fixes if f.major == v.major]
        return v < (same_line[0] if same_line else fixes[0])


def pins(text: str) -> dict[str, str]:
    """{canonical package name: version} for every exact == pin in a requirements file."""
    out = {}
    for line in (text or "").splitlines():
        m = _PIN.match(line)
        if m and not line.lstrip().startswith("#"):
            out[canonicalize_name(m.group(1))] = m.group(2)
    return out


@dataclass
class FakeWorld:
    """Callable like subprocess.run. One per run (it counts calls for the faults)."""

    advisories: list[Advisory] = field(default_factory=list)
    breaking_pins: set[tuple[str, str]] = field(default_factory=set)  # (package, version) that turn the tests red
    missing_versions: set[tuple[str, str]] = field(default_factory=set)  # (package, version) pip cannot find
    tests_red: bool = False  # the repo's own tests fail whatever the pins
    test_output_extra: str = ""  # untrusted text in the test output (injection scenarios)
    pip_error_extra: str = ""  # untrusted text in pip's error output (injection scenarios)
    fault: Fault | None = None
    calls: dict[str, int] = field(default_factory=lambda: {"trivy": 0, "pip": 0, "pytest": 0})
    log: list[tuple[str, int, bool]] = field(default_factory=list)  # (program, call number, faulted)

    # --- the subprocess.run stand-in ---------------------------------------------

    def __call__(self, cmd, cwd=None, timeout=None, **kwargs):
        cmd = [str(c) for c in cmd]
        program = self._program(cmd)
        if program is None:
            return subprocess.CompletedProcess(cmd, 5, "", "no tests ran")  # e.g. unittest fallback: nothing
        self.calls[program] += 1
        n = self.calls[program]
        faulted = self.fault is not None and self.fault.hits(program, n)
        self.log.append((program, n, faulted))
        if faulted:
            return self.fault.fire(cmd, timeout)
        if program == "trivy":
            return self._trivy(cmd)
        if program == "pip":
            return self._pip(cmd)
        return self._pytest(cmd, Path(cwd or "."))

    @staticmethod
    def _program(cmd: list[str]) -> str | None:
        if cmd and Path(cmd[0]).name.lower().startswith("trivy"):
            return "trivy"
        if "-m" in cmd:
            module = cmd[cmd.index("-m") + 1] if cmd.index("-m") + 1 < len(cmd) else ""
            if module == "pip":
                return "pip"
            if module == "pytest":
                return "pytest"
        return None

    # --- trivy -------------------------------------------------------------------

    def findings(self, root: Path) -> list[dict]:
        """What the scanner reports for every requirements.txt under root, in Trivy's JSON shape."""
        results = []
        for req in sorted(root.rglob("requirements.txt")):
            rel = req.relative_to(root)
            if any(part in SKIP_DIRS for part in rel.parts):
                continue
            installed = pins(req.read_text(encoding="utf-8", errors="replace"))
            vulns = []
            for adv in self.advisories:
                version = installed.get(canonicalize_name(adv.package))
                if version is not None and adv.affects(version):
                    vulns.append({"VulnerabilityID": adv.id, "PkgName": canonicalize_name(adv.package),
                                  "InstalledVersion": version, "FixedVersion": adv.fixed or "",
                                  "Severity": adv.severity, "Title": adv.title})
            if vulns:
                results.append({"Target": rel.as_posix(), "Type": "pip", "Vulnerabilities": vulns})
        return results

    def _trivy(self, cmd: list[str]) -> subprocess.CompletedProcess:
        root = Path(cmd[-1])
        report = {"SchemaVersion": 2, "ArtifactName": root.name, "Results": self.findings(root)}
        return subprocess.CompletedProcess(cmd, 0, json.dumps(report), "")

    # --- pip -----------------------------------------------------------------------

    def _pip(self, cmd: list[str]) -> subprocess.CompletedProcess:
        if "-r" not in cmd:
            return subprocess.CompletedProcess(cmd, 0, "", "")  # e.g. pip install -e .
        req = Path(cmd[cmd.index("-r") + 1])
        for name, version in pins(req.read_text(encoding="utf-8", errors="replace")).items():
            if (name, version) in self.missing_versions or self._absurd(version):
                err = (f"ERROR: Could not find a version that satisfies the requirement {name}=={version}\n"
                       f"ERROR: No matching distribution found for {name}=={version}\n")
                return subprocess.CompletedProcess(cmd, 1, "", err + self.pip_error_extra)
        return subprocess.CompletedProcess(cmd, 0, "", "")

    @staticmethod
    def _absurd(version: str) -> bool:
        try:
            return Version(version).major >= 50  # e.g. requests==99.0.0 does not exist
        except InvalidVersion:
            return True

    # --- pytest --------------------------------------------------------------------

    def _pytest(self, cmd: list[str], cwd: Path) -> subprocess.CompletedProcess:
        req = cwd / "requirements.txt"
        installed = pins(req.read_text(encoding="utf-8", errors="replace")) if req.is_file() else {}
        broken = sorted(f"{p}=={v}" for p, v in installed.items() if (p, v) in self.breaking_pins)
        extra = f"\n{self.test_output_extra}" if self.test_output_extra else ""
        if self.tests_red or broken:
            why = f"AttributeError after upgrading {', '.join(broken)}" if broken else "AssertionError"
            out = (f"F..\n=================================== FAILURES ===================================\n"
                   f"___________________________ test_get_returns_json ____________________________\n"
                   f"E   {why}{extra}\n"
                   f"=========================== short test summary info ============================\n"
                   f"FAILED tests/test_client.py::test_get_returns_json - {why}\n"
                   f"1 failed, 2 passed in 0.21s\n")
            return subprocess.CompletedProcess(cmd, 1, out, "")
        return subprocess.CompletedProcess(cmd, 0, f"...{extra}\n3 passed in 0.12s\n", "")
