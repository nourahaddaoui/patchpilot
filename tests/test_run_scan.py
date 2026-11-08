import json
import shutil
import subprocess

import pytest

from agent.tools import run_scan
from agent.tools.errors import ToolError
from tests.conftest import FIXTURES, TEMPLATE

REPORT = (FIXTURES / "trivy_requests_2_25_1.json").read_text()


def test_summarizes_findings_sorted(ctx, fake_runner):
    ctx.runner = fake_runner((0, REPORT, ""))
    out = json.loads(run_scan.run({}, ctx))
    assert out["clean"] is False and out["count"] == 3
    assert [v["id"] for v in out["vulnerabilities"]] == ["CVE-2023-32681", "CVE-2024-35195", "CVE-2024-47081"]
    assert out["vulnerabilities"][0] == {
        "id": "CVE-2023-32681", "package": "requests", "installed": "2.25.1", "fixed": "2.31.0",
        "severity": "MEDIUM", "title": "python-requests: Unintended leak of Proxy-Authorization header",
        "file": "requirements.txt",
    }


def test_command_is_offline_and_uses_cache(ctx, fake_runner):
    ctx.runner = fake_runner((0, REPORT, ""))
    run_scan.run({"path": "."}, ctx)
    cmd, kwargs = ctx.runner.calls[0]
    assert cmd[:2] == ["trivy", "fs"]
    assert "--skip-db-update" in cmd and str(ctx.trivy_cache) in cmd
    assert cmd[-1] == str(ctx.repo_root)
    assert kwargs["timeout"] == ctx.scan_timeout_s


def test_clean_scan(ctx, fake_runner):
    ctx.runner = fake_runner((0, json.dumps({"Results": [{"Target": "requirements.txt"}]}), ""))
    assert json.loads(run_scan.run({}, ctx)) == {"path": ".", "clean": True, "count": 0, "vulnerabilities": []}


def test_missing_fixed_version_is_null(ctx, fake_runner):
    report = {"Results": [{"Target": "requirements.txt", "Vulnerabilities": [
        {"VulnerabilityID": "CVE-X", "PkgName": "lib", "InstalledVersion": "1.0", "FixedVersion": "", "Severity": "HIGH"}]}]}
    ctx.runner = fake_runner((0, json.dumps(report), ""))
    assert json.loads(run_scan.run({}, ctx))["vulnerabilities"][0]["fixed"] is None


@pytest.mark.parametrize(
    "outcome, kind",
    [
        (FileNotFoundError(), "tool_down"),
        (subprocess.TimeoutExpired("trivy", 120), "timeout"),
        ((1, "", "FATAL: --skip-db-update cannot be specified on the first run"), "tool_down"),
        ((2, "", "boom"), "tool_down"),
        ((0, "not json", ""), "bad_output"),
        ((0, "[]", ""), "bad_output"),
    ],
)
def test_failures_become_tool_errors(ctx, fake_runner, outcome, kind):
    ctx.runner = fake_runner(outcome)
    with pytest.raises(ToolError) as e:
        run_scan.run({}, ctx)
    assert e.value.kind == kind


def test_bad_scan_paths(ctx):
    with pytest.raises(ToolError) as e:
        run_scan.run({"path": "../"}, ctx)
    assert e.value.kind == "not_allowed"
    with pytest.raises(ToolError) as e:
        run_scan.run({"path": "nope"}, ctx)
    assert e.value.kind == "not_found"


@pytest.mark.skipif(shutil.which("trivy") is None, reason="trivy not installed")
def test_real_scan_of_template_finds_requests_cves():
    from agent.tools.context import ToolContext

    ctx = ToolContext(repo_root=TEMPLATE)
    if not (ctx.trivy_cache / "db").exists():
        pytest.skip("Trivy DB not cached; run make setup")
    out = json.loads(run_scan.run({}, ctx))
    ids = {v["id"] for v in out["vulnerabilities"] if v["package"] == "requests"}
    assert "CVE-2023-32681" in ids
