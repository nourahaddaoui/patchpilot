import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from agent.tools.context import ToolContext

PROJECT_ROOT = Path(__file__).resolve().parents[1]
TEMPLATE = PROJECT_ROOT / "sandbox" / "template"
FIXTURES = Path(__file__).resolve().parent / "fixtures"


LIVE_VARS = (
    "PATCHPILOT_ISSUES", "PATCHPILOT_GITHUB_REPO", "GITHUB_TOKEN", "PATCHPILOT_ADMIN_TOKEN",
    "OPENAI_API_KEY", "OPENAI_BASE_URL", "PATCHPILOT_MODEL", "PATCHPILOT_ENGINE",
    "PATCHPILOT_PRICE_IN", "PATCHPILOT_PRICE_OUT",
)


def _no_network(*args, **kwargs):
    raise AssertionError("a test tried to reach the real GitHub API; inject a fake ctx.http instead")


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    """Tests never see the developer's .env and never reach GitHub.

    OpenAIModel() loads .env into os.environ; without this, github mode and the real
    token would leak into every later test and create_issue would call the live API.
    """
    from agent import model
    from agent.tools import create_issue

    monkeypatch.setattr(model, "DOTENV_PATH", None)  # every caller, however it imported load_dotenv
    monkeypatch.setattr(model, "load_dotenv", lambda *a, **k: None)
    for name in LIVE_VARS:
        monkeypatch.delenv(name, raising=False)
    for name in [k for k in os.environ if k.startswith(("LANGSMITH_", "LANGCHAIN_"))]:
        monkeypatch.delenv(name)  # no key, no endpoint, no project: tests can never send a trace
    monkeypatch.setenv("LANGSMITH_TRACING", "false")
    monkeypatch.setenv("LANGCHAIN_TRACING_V2", "false")
    monkeypatch.setattr(create_issue, "_urllib_http", _no_network)


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
