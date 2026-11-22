"""The target repo's code must never see a secret: run_tests and run_scan pass a scrubbed env."""

import json
import os
import sys

import pytest

from agent.tools import run_scan, run_tests
from agent.tools.context import ToolContext
from agent.tools.run_tests import clean_env, is_secret

FAKE_SECRETS = {
    "GITHUB_TOKEN": "ghp_fake",
    "GITHUB_ACTOR": "someone",
    "OPENAI_API_KEY": "sk-fake",
    "OPENAI_ORG": "org-fake",
    "LANGSMITH_ENDPOINT": "https://example.invalid",
    "PATCHPILOT_ADMIN_TOKEN": "ghp_admin_fake",
    "PATCHPILOT_ADMIN_REPO": "o/r",
    "AWS_SECRET_ACCESS_KEY": "aws-fake",
    "my_db_password": "hunter2",
    "Some_Api_Key": "lower-and-mixed-case",
    "NPM_TOKEN": "npm-fake",
}
NEEDED = {
    "PATH": "/usr/bin:/bin",
    "HOME": "/home/someone",
    "HTTPS_PROXY": "http://proxy.local:3128",
    "http_proxy": "http://proxy.local:3128",
    "NO_PROXY": "localhost",
    "SSL_CERT_FILE": "/etc/ssl/cert.pem",
    "REQUESTS_CA_BUNDLE": "/etc/ssl/cert.pem",
    "PIP_CERT": "/etc/ssl/cert.pem",
    "PATCHPILOT_TRIVY_CACHE": "/data/trivy",
    "PATCHPILOT_ISSUES": "github",
}


@pytest.fixture
def secrets_set(monkeypatch):
    for k, v in {**FAKE_SECRETS, **NEEDED}.items():
        monkeypatch.setenv(k, v)


def _assert_scrubbed(env: dict) -> None:
    if os.name == "nt":  # Windows env names are case-insensitive and stored upper-case
        env = {k.upper(): v for k, v in env.items()}
        fold = str.upper
    else:
        fold = str
    for name in FAKE_SECRETS:
        assert fold(name) not in env, name
    assert not any(v in FAKE_SECRETS.values() for v in env.values())
    for name, value in NEEDED.items():
        assert env.get(fold(name)) == value, name


def test_is_secret_rules():
    for name in FAKE_SECRETS:
        assert is_secret(name), name
    for name in NEEDED:
        assert not is_secret(name), name


def test_clean_env_does_not_touch_the_original():
    source = {"GITHUB_TOKEN": "x", "PATH": "/bin"}
    assert clean_env(source) == {"PATH": "/bin"}
    assert source == {"GITHUB_TOKEN": "x", "PATH": "/bin"}


def test_run_tests_subprocesses_get_no_secrets(secrets_set, ctx, fake_runner):
    ctx.install_deps = True
    ctx.runner = fake_runner((0, "", ""), (0, "1 passed in 0.01s", ""))
    out = json.loads(run_tests.run({}, ctx))
    assert out["green"] is True
    assert len(ctx.runner.calls) == 2  # pip install, then pytest
    for _cmd, kwargs in ctx.runner.calls:
        _assert_scrubbed(kwargs["env"])


def test_run_scan_subprocess_gets_no_secrets(secrets_set, ctx, fake_runner):
    ctx.runner = fake_runner((0, json.dumps({"Results": []}), ""))
    assert json.loads(run_scan.run({}, ctx))["clean"] is True
    _cmd, kwargs = ctx.runner.calls[0]
    _assert_scrubbed(kwargs["env"])


def test_real_pytest_run_cannot_read_secrets_and_still_works(secrets_set, monkeypatch, tmp_path):
    """End to end: a test in the target repo looks for the secrets and finds none, and the run is green."""
    monkeypatch.setenv("PATH", NEEDED["PATH"] + ":" + os.path.dirname(sys.executable))
    repo = tmp_path / "r"
    (repo / "tests").mkdir(parents=True)
    names = sorted(FAKE_SECRETS)
    (repo / "tests" / "test_spy.py").write_text(
        "import os\n"
        f"def test_no_secrets():\n    assert [n for n in {names!r} if n in os.environ] == []\n"
        "def test_needed_kept():\n    assert os.environ.get('PATCHPILOT_TRIVY_CACHE') == '/data/trivy'\n"
    )
    ctx = ToolContext(repo_root=repo, test_python=sys.executable, install_deps=False)
    out = json.loads(run_tests.run({}, ctx))
    assert out["green"] is True, out["output_tail"]
    assert out["passed"] == 2
