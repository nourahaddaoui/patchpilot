import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from agent.tools.context import ToolContext

PROJECT_ROOT = Path(__file__).resolve().parents[1]
TEMPLATE = PROJECT_ROOT / "sandbox" / "template"
FIXTURES = Path(__file__).resolve().parent / "fixtures"


@pytest.fixture
def template_repo(tmp_path):
    """A throwaway copy of the target repo template."""
    dest = tmp_path / "repo"
    shutil.copytree(TEMPLATE, dest)
    return dest


@pytest.fixture
def ctx(template_repo):
    return ToolContext(repo_root=template_repo, test_python=sys.executable, install_deps=False)


class FakeRunner:
    """Stands in for subprocess.run: returns canned results or raises, and records calls."""

    def __init__(self, *outcomes):
        self.outcomes = list(outcomes)
        self.calls = []

    def __call__(self, cmd, **kwargs):
        self.calls.append((cmd, kwargs))
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, BaseException):
            raise outcome
        returncode, stdout, stderr = outcome
        return subprocess.CompletedProcess(cmd, returncode, stdout, stderr)


@pytest.fixture
def fake_runner():
    return FakeRunner
