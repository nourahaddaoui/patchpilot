"""Real repos as targets: fetched into sandbox/sources, never edited in place. No network."""

import shutil
import subprocess

import pytest

from agent import sandbox, targets


def git(cwd, *args):
    subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", *args], cwd=cwd, check=True,
                   capture_output=True)


@pytest.fixture
def real_repo(tmp_path):
    if shutil.which("git") is None:
        pytest.skip("git not installed")
    repo = tmp_path / "my-app"
    repo.mkdir()
    (repo / "requirements.txt").write_text("requests==2.25.1\n")
    (repo / "app.py").write_text("x = 1\n")
    git(repo, "init", "-q")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "init")
    (repo / "uncommitted.txt").write_text("work in progress")  # not part of the clone
    (repo / ".venv").mkdir()
    return repo


@pytest.mark.parametrize("target, want", [
    ("https://github.com/miguelgrinberg/microblog", "miguelgrinberg-microblog"),
    ("https://github.com/owner/repo.git", "owner-repo"),
    ("git@github.com:owner/repo.git", "owner-repo"),
    ("C:/code/My App", "My-App"),
])
def test_slug(target, want):
    assert targets.slug(target) == want


def test_local_git_repo_is_cloned_not_used_in_place(real_repo, tmp_path):
    sources = tmp_path / "sources"
    src = targets.fetch(str(real_repo), sources=sources)
    assert src == sources / "my-app"
    assert (src / "requirements.txt").read_text() == "requests==2.25.1\n"
    assert not (src / "uncommitted.txt").exists() and not (src / ".venv").exists()


def test_fetch_again_replaces_the_previous_clone(real_repo, tmp_path):
    sources = tmp_path / "sources"
    targets.fetch(str(real_repo), sources=sources)
    (sources / "my-app" / "stale.txt").write_text("old")
    targets.fetch(str(real_repo), sources=sources)
    assert not (sources / "my-app" / "stale.txt").exists()


def test_sandbox_from_a_real_repo_leaves_the_original_untouched(real_repo, tmp_path):
    src = targets.fetch(str(real_repo), sources=tmp_path / "sources")
    work = sandbox.reset(src, tmp_path / "work")
    assert sandbox.is_sandbox(work)
    (work / "requirements.txt").write_text("requests==2.32.4\n")
    sandbox.commit_fix(work, "fix")
    assert (real_repo / "requirements.txt").read_text() == "requests==2.25.1\n"
    status = subprocess.run(["git", "status", "--porcelain"], cwd=real_repo, capture_output=True, text=True).stdout
    assert "requirements.txt" not in status


def test_missing_target_is_a_clear_error(tmp_path):
    with pytest.raises(targets.TargetError, match="not a URL or an existing folder"):
        targets.fetch(str(tmp_path / "nope"), sources=tmp_path / "sources")


def test_bad_url_is_a_clear_error(tmp_path):
    with pytest.raises(targets.TargetError, match="could not clone"):
        targets.fetch("https://invalid.invalid/none/none", sources=tmp_path / "sources", timeout_s=30)
