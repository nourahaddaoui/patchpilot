import json

from agent.calllog import CallLogger
from agent.tools import registry
from agent.tools.registry import TOOLS, call_tool, tool_definitions


def test_three_read_tools_with_strict_schemas():
    defs = tool_definitions()
    assert [d["name"] for d in defs] == ["run_scan", "read_file", "run_tests"]
    for d in defs:
        assert d["input_schema"]["type"] == "object"
        assert d["input_schema"]["additionalProperties"] is False
        assert d["description"]
    assert not any(t.writes for t in TOOLS.values())


def test_every_call_is_logged_with_input_output_time(ctx, tmp_path):
    logger = CallLogger(run_id="t1", log_dir=tmp_path)
    call_tool("read_file", {"path": "requirements.txt"}, ctx, logger, step=1)
    call_tool("read_file", {"path": "nope.txt"}, ctx, logger, step=2)
    records = logger.read()
    assert [r["step"] for r in records] == [1, 2]
    first = records[0]
    assert first["run_id"] == "t1" and first["tool"] == "read_file"
    assert first["input"] == {"path": "requirements.txt"}
    assert first["ok"] is True and first["output"].strip() == "requests==2.25.1"
    assert isinstance(first["duration_ms"], float) and first["ts"]
    assert records[1]["ok"] is False and records[1]["error_type"] == "not_allowed"


def test_bad_arguments_do_not_run_the_tool(ctx, monkeypatch):
    called = []
    tool = TOOLS["read_file"]
    monkeypatch.setitem(TOOLS, "read_file", tool.__class__(tool.name, tool.description, tool.input_schema, lambda a, c: called.append(1) or "x"))
    res = call_tool("read_file", {"file": "requirements.txt"}, ctx)
    assert not res.ok and res.error_type == "bad_arguments"
    assert "'path' is required" in res.content and "unexpected argument 'file'" in res.content
    assert called == []


def test_unknown_tool(ctx):
    res = call_tool("delete_repo", {}, ctx)
    assert not res.ok and res.error_type == "unknown_tool"
    assert "run_scan, read_file, run_tests" in res.content


def test_unexpected_exception_never_leaks_a_stack_trace(ctx, monkeypatch):
    def boom(args, c):
        raise KeyError("internal detail /secret/path")

    tool = TOOLS["run_scan"]
    monkeypatch.setitem(TOOLS, "run_scan", tool.__class__(tool.name, tool.description, tool.input_schema, boom))
    res = call_tool("run_scan", {}, ctx)
    assert not res.ok and res.error_type == "internal"
    assert "Traceback" not in res.content and "/secret/path" not in res.content
    assert res.content.startswith("ERROR (run_scan): internal tool failure (KeyError)")


def test_tool_errors_are_plain_text(ctx, fake_runner):
    ctx.runner = fake_runner(FileNotFoundError())
    res = call_tool("run_scan", {}, ctx)
    assert res.content == (
        "ERROR (run_scan): the scanner (trivy) is not installed or not on PATH. "
        "Scanning is unavailable; stop and report it."
    )


def test_successful_scan_through_registry_is_json(ctx, fake_runner):
    from tests.conftest import FIXTURES

    ctx.runner = fake_runner((0, (FIXTURES / "trivy_requests_2_25_1.json").read_text(), ""))
    res = call_tool("run_scan", {}, ctx)
    assert res.ok and json.loads(res.content)["count"] == 3
