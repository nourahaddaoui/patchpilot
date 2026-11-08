import json
import subprocess
import sys

import pytest

from agent.tools import run_tests
from agent.tools.context import ToolContext
from agent.tools.errors import ToolError


def _repo(tmp_path, body):
    repo = tmp_path / "r"
    (repo / "tests").mkdir(parents=True)
    (repo / "tests" / "test_x.py").write_text(body)
    return ToolContext(repo_root=repo, test_python=sys.executable, install_deps=False)


def test_green_run(tmp_path):
    ctx = _repo(tmp_path, "def test_a():\n    assert True\n\ndef test_b():\n    assert True\n")
    out = json.loads(run_tests.run({}, ctx))
    assert out["green"] is True and out["exit_code"] == 0
    assert out["passed"] == 2 and out["failed"] == 0
    assert not (ctx.repo_root / ".pytest_cache").exists()


def test_failing_tests_are_a_result_not_an_error(tmp_path):
    ctx = _repo(tmp_path, "def test_a():\n    assert True\n\ndef test_b():\n    assert 1 == 2\n")
    out = json.loads(run_tests.run({}, ctx))
    assert out["green"] is False and out["exit_code"] == 1
    assert out["passed"] == 1 and out["failed"] == 1
    assert "assert 1 == 2" in out["output_tail"]


def test_no_tests_collected_is_not_green(tmp_path):
    ctx = _repo(tmp_path, "x = 1\n")
    out = json.loads(run_tests.run({}, ctx))
    assert out["green"] is False and out["exit_code"] == 5


def test_missing_interpreter(tmp_path):
    ctx = _repo(tmp_path, "def test_a():\n    pass\n")
    ctx.test_python = str(tmp_path / "no" / "python")
    with pytest.raises(ToolError) as e:
        run_tests.run({}, ctx)
    assert e.value.kind == "tool_down"


def test_timeout(ctx, fake_runner):
    ctx.runner = fake_runner(subprocess.TimeoutExpired("pytest", 120))
    with pytest.raises(ToolError) as e:
        run_tests.run({}, ctx)
    assert e.value.kind == "timeout"


def test_install_runs_first_and_failure_stops_tests(ctx, fake_runner):
    ctx.install_deps = True
    ctx.runner = fake_runner((1, "", "ERROR: No matching distribution found for requests==9.9.9"))
    with pytest.raises(ToolError) as e:
        run_tests.run({}, ctx)
    assert e.value.kind == "install_failed"
    assert "requests==9.9.9" in e.value.message
    assert len(ctx.runner.calls) == 1
    cmd, _ = ctx.runner.calls[0]
    assert cmd[1:5] == ["-m", "pip", "install", "-q"]


def test_install_then_pytest(ctx, fake_runner):
    ctx.install_deps = True
    ctx.runner = fake_runner((0, "", ""), (0, "...\n3 passed in 0.10s\n", ""))
    out = json.loads(run_tests.run({}, ctx))
    assert out["green"] is True and out["passed"] == 3
    assert ctx.runner.calls[1][0][1:3] == ["-m", "pytest"]


def test_parse_counts():
    line = "==== 2 failed, 5 passed, 1 error in 0.3s ===="
    assert run_tests.parse_counts(line) == {"passed": 5, "failed": 2, "errors": 1, "summary": "2 failed, 5 passed, 1 error in 0.3s"}
