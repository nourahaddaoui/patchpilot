import shutil
import subprocess

import pytest

from agent import sandbox
from tests.conftest import TEMPLATE

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="git not installed")


def _git(repo, *args):
    return subprocess.run(["git", *args], cwd=repo, capture_output=True, text=True, check=True).stdout.strip()


def test_reset_builds_a_marked_clone_on_the_fix_branch(tmp_path):
    work = sandbox.reset(TEMPLATE, tmp_path / "work")
    assert sandbox.is_sandbox(work)
    assert (work / "requirements.txt").read_text() == (TEMPLATE / "requirements.txt").read_text()
    assert _git(work, "branch", "--show-current") == sandbox.FIX_BRANCH
    assert _git(work, "log", "--format=%s", "main") == "Baseline from template"
    assert _git(work, "status", "--porcelain") == ""


def test_reset_discards_agent_changes_and_is_repeatable(tmp_path):
    work = sandbox.reset(TEMPLATE, tmp_path / "work")
    (work / "requirements.txt").write_text("requests==2.32.4\n")
    (work / "stray.py").write_text("x = 1\n")
    sandbox.reset(TEMPLATE, work)
    assert (work / "requirements.txt").read_text() == "requests==2.25.1\n"
    assert not (work / "stray.py").exists()
    assert _git(work, "status", "--porcelain") == ""


def test_reset_refuses_to_delete_a_directory_it_did_not_create(tmp_path):
    precious = tmp_path / "precious"
    precious.mkdir()
    (precious / "keep.txt").write_text("do not delete")
    with pytest.raises(RuntimeError, match="not a PatchPilot sandbox"):
        sandbox.reset(TEMPLATE, precious)
    assert (precious / "keep.txt").exists()


def test_reset_from_another_source(tmp_path):
    variant = tmp_path / "variant"
    shutil.copytree(TEMPLATE, variant)
    (variant / "requirements.txt").write_text("requests==2.31.0\n")
    work = sandbox.reset(variant, tmp_path / "work")
    assert (work / "requirements.txt").read_text() == "requests==2.31.0\n"


def test_template_is_never_a_sandbox():
    assert not sandbox.is_sandbox(TEMPLATE)
