"""--issues github files on the --target repo; a bad setup fails before anything is cloned."""

import pytest

from agent import __main__ as cli
from agent import targets


@pytest.mark.parametrize("target, want", [
    ("https://github.com/oumaymaabdelkefi/eddium", "oumaymaabdelkefi/eddium"),
    ("https://github.com/o/r.git", "o/r"),
    ("https://github.com/o/r/", "o/r"),
    ("git@github.com:o/my.repo.git", "o/my.repo"),
    ("https://gitlab.com/o/r", None),
    ("C:/code/r", None),
    (None, None),
    ("https://github.com/o/r/issues", None),
])
def test_github_repo(target, want):
    assert targets.github_repo(target) == want


@pytest.fixture
def no_clone(monkeypatch):
    def boom(*a, **k):
        raise AssertionError("must not clone when the setup is invalid")
    monkeypatch.setattr(targets, "fetch", boom)
    monkeypatch.setattr(cli, "load_dotenv", lambda: None)


def test_github_issues_need_a_token(no_clone, monkeypatch):
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    with pytest.raises(cli.SetupError, match="GITHUB_TOKEN"):
        cli.prepare("https://github.com/o/r", issues="github")


def test_github_issues_need_a_github_target(no_clone, monkeypatch):
    monkeypatch.setenv("GITHUB_TOKEN", "ghp_fake")
    with pytest.raises(cli.SetupError, match="GitHub URL"):
        cli.prepare("https://gitlab.com/o/r", issues="github")


def test_issue_repo_is_the_target(monkeypatch, tmp_path):
    monkeypatch.setattr(cli, "load_dotenv", lambda: None)
    monkeypatch.setenv("GITHUB_TOKEN", "ghp_fake")
    monkeypatch.setenv("PATCHPILOT_GITHUB_REPO", "team/demo")  # the target wins
    monkeypatch.setattr(targets, "fetch", lambda t: tmp_path / "src")
    monkeypatch.setattr(targets, "target_python", lambda name: "python")
    monkeypatch.setattr(cli.sandbox, "reset", lambda *a, **k: None)
    monkeypatch.setattr(cli.sandbox, "is_sandbox", lambda repo: True)
    p = cli.prepare("https://github.com/someone/app", issues="github", say=lambda s: None)
    assert p.ctx.github_mode and p.ctx.github_repo == "someone/app"
    assert "GitHub issues on someone/app" in cli.banner(p)


def test_local_issues_are_the_default(monkeypatch, tmp_path):
    monkeypatch.setattr(cli, "load_dotenv", lambda: None)
    monkeypatch.delenv("PATCHPILOT_ISSUES", raising=False)
    monkeypatch.setattr(cli.sandbox, "reset", lambda *a, **k: None)
    monkeypatch.setattr(cli.sandbox, "is_sandbox", lambda repo: True)
    p = cli.prepare(say=lambda s: None)
    assert not p.ctx.github_mode and "local issues" in cli.banner(p)
