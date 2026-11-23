"""web/runs.py: one eval scenario run by the real agent, with the checkpoint answered from the browser."""

import os
import shutil
import time

import pytest

from web import runs
from web.runs import BrowserHuman, RunManager, RunRefused, demo_github_token, models_for
from eval.scenarios import BY_ID

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="git not installed")


def wait(run, until, timeout=30):
    deadline = time.time() + timeout
    while time.time() < deadline:
        view = run.view()
        if until(view):
            return view
        time.sleep(0.02)
    raise AssertionError(f"timed out; last status {run.view()['status']}")


def waiting(v):
    return v["status"] == "waiting"


def done(v):
    return v["status"] in ("finished", "error")


@pytest.fixture
def manager():
    m = RunManager(max_active=2, answer_timeout_s=10)
    yield m
    m.shutdown()


def test_approved_fix_reaches_done_and_the_page_saw_the_diff(manager):
    run = manager.start("fix-01-one-cve")
    v = wait(run, waiting)
    assert "Apply this edit?" in v["pending"]["question"]
    assert "+requests==" in v["pending"]["text"]  # the full diff, as the terminal shows it
    assert manager.answer(run.id, True)
    v = wait(run, done)
    assert v["status"] == "finished" and v["result"]["stop_reason"] == "done" and v["result"]["success"]
    assert v["result"]["requirements_after"] != v["result"]["requirements_before"]
    kinds = [row["kind"] for row in v["timeline"]]
    assert kinds[0] == "model" and "checkpoint" in kinds and kinds[-1] == "stop"


def test_deny_writes_nothing_and_stops(manager):
    run = manager.start("fix-01-one-cve")
    wait(run, waiting)
    manager.answer(run.id, False)
    v = wait(run, done)
    assert v["result"]["stop_reason"] == "approval_denied"
    assert v["result"]["requirements_after"] == v["result"]["requirements_before"]


def test_no_answer_in_time_is_a_no(manager):
    manager.answer_timeout_s = 0.2
    run = manager.start("fix-01-one-cve")
    v = wait(run, done)
    assert v["result"]["stop_reason"] == "approval_denied"
    assert run.human.decisions[0]["answer"] == "<no answer in time>"


def test_an_answer_with_nothing_waiting_is_refused(manager):
    run = manager.start("lim-03-never-finishes")  # a scripted model that never writes
    wait(run, done)
    assert manager.answer(run.id, True) is False
    assert run.view()["result"]["stop_reason"] == "step_limit"


def test_compromised_model_is_stopped_on_an_injection_scenario(manager):
    run = manager.start("inj-01-scan-title-downgrade", model="compromised")
    v = wait(run, lambda v: waiting(v) or done(v))
    while waiting(v):  # the tool guard refused the downgrade; the human only ever sees safe pins
        assert "+requests==2.0.0" not in v["pending"]["text"]
        manager.answer(run.id, True)
        v = wait(run, lambda v: waiting(v) or done(v))
    assert v["result"]["success"]
    assert "requests==2.0.0" not in v["result"]["requirements_after"]
    tools = [row for row in v["timeline"] if row["kind"] == "tool"]
    assert any(row["tool"] == "edit_file" and not row["ok"] for row in tools)  # the attack call, refused


def test_github_flow_runs_against_the_fake_github_only(manager, monkeypatch):
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    run = manager.start("fix-14-github-flow")
    for _ in range(4):  # tracking issue, edit, pull request, merge
        v = wait(run, lambda v: waiting(v) or done(v))
        if done(v):
            break
        manager.answer(run.id, True)
    v = wait(run, done)
    assert v["result"]["stop_reason"] == "done"
    assert "GITHUB_TOKEN" not in os.environ  # the demo token is gone once no run needs it


@pytest.mark.parametrize("sid, model, message", [
    ("nope", "rules", "unknown scenario"),
    ("fix-01-one-cve", "compromised", "not available"),  # only injection scenarios have an attack to obey
    ("fix-01-one-cve", "gpt-4o-mini", "not available"),   # paid models are off unless the owner turns them on
])
def test_refused_requests(manager, sid, model, message):
    with pytest.raises(RunRefused, match=message):
        manager.start(sid, model=model)


def test_paid_models_only_when_the_owner_lists_them(monkeypatch):
    assert "gpt-4o-mini" not in models_for(BY_ID["fix-01-one-cve"])
    monkeypatch.setenv("PATCHPILOT_WEB_LLM_MODELS", "gpt-4o-mini")
    assert "gpt-4o-mini" in models_for(BY_ID["fix-01-one-cve"])
    assert models_for(BY_ID["lim-03-never-finishes"]) == ["scripted"]


def test_busy_server_refuses_new_runs(manager):
    a = manager.start("fix-01-one-cve")
    b = manager.start("fix-01-one-cve")
    wait(a, waiting), wait(b, waiting)
    with pytest.raises(RunRefused, match="busy"):
        manager.start("fix-01-one-cve")


def test_a_double_click_answers_one_question_only(manager):
    run = manager.start("hum-02-changes-mind")  # two edits: the second must get its own answer
    wait(run, waiting)
    assert manager.answer(run.id, True) is True
    assert manager.answer(run.id, True) is False  # second click: nothing is waiting any more
    v = wait(run, lambda v: waiting(v) or done(v))
    assert waiting(v), "the second edit must be asked, not approved by the extra click"
    manager.answer(run.id, False)
    v = wait(run, done)
    assert v["result"]["stop_reason"] == "approval_denied"
    assert [d["answer"] for d in run.human.decisions] == ["y", "n"]


def test_browser_human_rules():
    h = BrowserHuman(timeout_s=5)
    assert h.answer(True) is False  # nothing asked yet
    h.close()
    with pytest.raises(EOFError):
        h.ask("Apply this edit? [y/n] ")


def test_demo_token_never_replaces_a_real_one(monkeypatch):
    monkeypatch.setenv("GITHUB_TOKEN", "real-token-value")
    with demo_github_token(True):
        assert os.environ["GITHUB_TOKEN"] == "real-token-value"
    assert os.environ["GITHUB_TOKEN"] == "real-token-value"
