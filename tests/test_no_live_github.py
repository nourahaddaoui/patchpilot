"""Regression for the 2026-11-19 incident (docs/postmortem_notes.md).

The suite once ran with github mode and a real token inherited from .env, and
created real issues on the demo repo. This test starts a child pytest whose
environment already holds a real-looking GITHUB_TOKEN, PATCHPILOT_ISSUES=github
and a repo, runs the test files involved in the incident in the same order, and
blocks + records every outbound connection. The child must pass and the record
must be empty: zero real HTTP calls.
"""

import os
import subprocess
import sys

from tests.conftest import PROJECT_ROOT

INCIDENT_FILES = [  # test_model.py first: it used to load .env into os.environ
    "tests/test_model.py",
    "tests/test_rules_model.py",
    "tests/test_issue_flow.py",
    "tests/test_loop.py",
]


def test_live_looking_credentials_cause_zero_real_http_calls(tmp_path):
    record = tmp_path / "connections.log"
    record.write_text("")
    env = dict(os.environ)
    env.update({
        "GITHUB_TOKEN": "ghp_" + "A1b2C3d4E5f6G7h8I9j0" * 2,  # real format, not a real token
        "PATCHPILOT_ISSUES": "github",
        "PATCHPILOT_GITHUB_REPO": "example-owner/not-a-real-repo",
        "NETGUARD_LOG": str(record),
    })
    proc = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", "-p", "tests._netguard", *INCIDENT_FILES],
        cwd=PROJECT_ROOT, env=env, capture_output=True, text=True, timeout=300,
    )
    assert proc.returncode == 0, proc.stdout[-3000:] + proc.stderr[-2000:]
    assert record.read_text() == "", f"tests tried to reach the network:\n{record.read_text()}"


def test_netguard_really_blocks(tmp_path):
    """The guard itself works: a deliberate connection is refused and recorded."""
    record = tmp_path / "connections.log"
    code = ("import tests._netguard, urllib.request\n"
            "try:\n    urllib.request.urlopen('https://api.github.com', timeout=5)\n"
            "except Exception as e:\n    print(type(e).__name__)\n")
    proc = subprocess.run([sys.executable, "-c", code], cwd=PROJECT_ROOT, capture_output=True, text=True,
                          env={**os.environ, "NETGUARD_LOG": str(record)}, timeout=60)
    assert "api.github.com" in record.read_text(), proc.stdout + proc.stderr
