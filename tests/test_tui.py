"""Terminal UI: the Approve/Deny buttons are the checkpoint's answer; the timeline follows the log.

Runs the app headless with Textual's pilot and a fake run (no Trivy, no network).
"""

import asyncio
from types import SimpleNamespace

import pytest

from agent import __main__ as cli
from agent.calllog import CallLogger
from agent.stop_rule import Limits
from agent.tools.context import ToolContext
from agent.tui import LiveLogger, PatchPilotApp, describe

ISSUE = {"tool": "create_issue", "mode": "github", "repo": "o/r", "title": "Bump requests", "body": "4 CVEs"}


def fake_run(tmp_path, seen):
    def prepare(logger, say, target, model_name, engine, issues):
        seen["params"] = dict(target=target, model=model_name, engine=engine, issues=issues)
        say("Fetching ...")
        logger.log_dir = tmp_path
        return cli.Prepared(SimpleNamespace(name=model_name), ToolContext(repo_root=tmp_path),
                            Limits(), logger, engine, target or "sandbox/template")

    def execute(p):
        p.logger.log({"event": "start", "model": p.model.name})
        p.logger.log({"event": "model", "step": 1, "tool_calls": [{"name": "create_issue", "arguments": "{}"}],
                      "total_cost_usd": 0.0012})
        seen["approved"] = p.ctx.approve(dict(ISSUE))  # blocks until the human clicks
        p.logger.log({"step": 1, "tool": "create_issue", "ok": seen["approved"], "duration_ms": 5})
        p.logger.log({"event": "stop", "reason": "handed_off" if seen["approved"] else "approval_denied",
                      "steps": 1, "cost_usd": 0.0012})
        return SimpleNamespace()

    def summary(result, p):
        return ["Stop reason: done (fake)"]

    return dict(prepare=prepare, execute=execute, summary=summary)


async def drive(app, clicks):
    async with app.run_test(size=(140, 40)) as pilot:
        await pilot.click("#run")
        for button in clicks:
            for _ in range(100):  # wait until the checkpoint asks
                await pilot.pause(0.05)
                if app.waiting:
                    break
            assert app.waiting, "the checkpoint never asked"
            await pilot.click(button)
        for _ in range(100):
            await pilot.pause(0.05)
            if not app.running:
                break
        return app


@pytest.fixture
def logs(tmp_path, monkeypatch):
    monkeypatch.setattr("agent.calllog.DEFAULT_LOG_DIR", tmp_path / "logs")
    return tmp_path


@pytest.mark.parametrize("button, approved, reason", [
    ("#approve", True, "handed_off"),
    ("#deny", False, "approval_denied"),
])
def test_buttons_answer_the_checkpoint(logs, button, approved, reason):
    seen = {}
    app = PatchPilotApp(target="https://github.com/o/r", issues="github", **fake_run(logs, seen))
    asyncio.run(drive(app, [button]))
    assert seen["approved"] is approved
    assert seen["params"] == {"target": "https://github.com/o/r", "model": "rules", "engine": "graph",
                              "issues": "github"}
    assert app.stop_reason == reason and app.steps == 1 and app.cost == pytest.approx(0.0012)
    assert not app.running and not app.waiting


def test_quitting_while_asked_is_a_no(logs):
    seen = {}
    app = PatchPilotApp(**fake_run(logs, seen))

    async def go():
        async with app.run_test() as pilot:
            await pilot.click("#run")
            for _ in range(100):
                await pilot.pause(0.05)
                if app.waiting:
                    break
            assert app.waiting
        # app has exited; the worker got "n" and finishes on its own
    asyncio.run(go())
    import time
    for _ in range(100):
        if "approved" in seen:
            break
        time.sleep(0.05)
    assert seen.get("approved") is False


def test_setup_error_is_shown_not_raised(logs):
    def prepare(**kw):
        raise cli.SetupError("GitHub issues need GITHUB_TOKEN in .env")

    app = PatchPilotApp(prepare=prepare)
    asyncio.run(drive(app, []))
    assert not app.running


def test_live_logger_writes_jsonl_and_forwards(tmp_path):
    got = []
    log = LiveLogger(on_record=got.append, log_dir=tmp_path)
    log.log({"event": "start", "model": "rules"})
    assert CallLogger(log.run_id, tmp_path).read()[0]["event"] == "start"
    assert got[0]["event"] == "start" and got[0]["run_id"] == log.run_id


def test_live_logger_survives_a_broken_listener(tmp_path):
    def boom(_):
        raise RuntimeError("ui gone")

    log = LiveLogger(on_record=boom, log_dir=tmp_path)
    log.log({"event": "start"})
    assert len(log.read()) == 1


@pytest.mark.parametrize("record, expect", [
    ({"event": "model", "step": 2, "tool_calls": [{"name": "run_scan", "arguments": "{}"}]}, "model chose run_scan"),
    ({"step": 2, "tool": "run_scan", "ok": False, "error_type": "timeout", "duration_ms": 3}, "ERROR timeout"),
    ({"event": "checkpoint", "approved": True, "proposal": {"package": "requests"}}, "APPROVED: requests"),
    ({"event": "stop", "reason": "done", "steps": 5, "cost_usd": 0}, "STOP     done"),
    ({"event": "checkpoint", "approved": False, "proposal": {"title": "evil\x1b[2Jtitle"}}, "DENIED: evil[2Jtitle"),
])
def test_describe(record, expect):
    assert expect in describe(record)
