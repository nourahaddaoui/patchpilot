"""LangSmith tracing: opt-in, one trace per run, secrets redacted, nothing ever sent from tests.

Traced runs here use tracing.make_client() (the same redacting client production uses)
on a recording session: every request body the client would upload is captured in memory
and checked. Nothing goes over the network.
"""

import json
import os
import sys

import pytest

from agent import tracing
from agent.checkpoint import Checkpoint, ScriptedHuman
from agent.graph import run_graph
from agent.loop import run
from agent.model import ScriptedModel
from agent.stop_rule import Limits, StopReason
from agent.tools.context import ToolContext
from agent.tools.run_tests import clean_env, is_secret
from agent.tracing import REDACTED, enabled, redact, run_traced
from tests.test_loop import FIX, logger, tool_records, work, world  # noqa: F401  (fixtures)

FAKE_GH = "ghp_FAKEfakeFAKEfake1234567890"
FAKE_OPENAI = "sk-proj-FAKEfakeFAKEfake0987654321"


class FakeResponse:
    status_code, ok, headers, text, content = 200, True, {}, "{}", b"{}"

    def json(self):
        return {}

    def raise_for_status(self):
        pass


class RecordingSession:
    """Stands in for the HTTP session: records method, url and body, answers 200, sends nothing."""

    def __init__(self):
        self.calls = []

    def request(self, method, url, *a, **k):
        body = k.get("data") if k.get("data") is not None else k.get("json")
        if isinstance(body, (bytes, bytearray)):
            body = body.decode("utf-8", "replace")
        self.calls.append((method, url, body if isinstance(body, str) else json.dumps(body, default=str)))
        return FakeResponse()

    def close(self):
        pass

    def mount(self, *a, **k):
        pass


@pytest.fixture
def client():
    session = RecordingSession()
    c = tracing.make_client(api_url="http://langsmith.invalid", api_key="test", info={},
                            auto_batch_tracing=False, session=session)
    return c, session


def walk(tree):
    yield tree
    for child in tree.child_runs or []:
        yield from walk(child)


# --- opt-in ------------------------------------------------------------------------

@pytest.mark.parametrize("env, on", [
    ({}, False),
    ({"LANGSMITH_TRACING": "true"}, False),  # no key: off
    ({"LANGSMITH_API_KEY": "k"}, False),  # key but not asked for: off
    ({"LANGSMITH_TRACING": "true", "LANGSMITH_API_KEY": "k"}, True),
    ({"LANGSMITH_TRACING": "false", "LANGSMITH_API_KEY": "k"}, False),
])
def test_tracing_is_opt_in(env, on):
    assert enabled(env) is on


def test_tests_run_with_tracing_off_and_no_langsmith_credentials():
    assert enabled() is False
    present = any(k.startswith(("LANGSMITH_API", "LANGCHAIN_API", "LANGSMITH_ENDPOINT")) for k in os.environ)
    assert not present


def test_env_scrub_drops_langsmith_for_target_tests():
    env = {"LANGSMITH_API_KEY": "x", "LANGSMITH_TRACING": "true", "LANGSMITH_ENDPOINT": "e",
           "LANGSMITH_PROJECT": "p", "LANGCHAIN_API_KEY": "y", "PATH": "/bin"}
    assert all(is_secret(k) for k in env if k != "PATH")
    assert clean_env(env) == {"PATH": "/bin"}


# --- redaction -------------------------------------------------------------------------

def test_redact_removes_secret_env_values_tokens_and_url_credentials(monkeypatch):
    monkeypatch.setenv("SOME_SERVICE_PASSWORD", "hunter2hunter2")
    out = redact({
        "a": "pw hunter2hunter2 here",
        "b": [f"token {FAKE_GH}", f"key {FAKE_OPENAI}", "Authorization: Bearer abc.def"],
        "c": "https://user:secret@github.com/o/r.git",
        "d": object(), "n": 3, "ok": True, "none": None,
    })
    text = json.dumps(out)
    for secret in ("hunter2hunter2", FAKE_GH, FAKE_OPENAI, "abc.def", "user:secret"):
        assert secret not in text, secret
    assert out["c"] == f"https://{REDACTED}@github.com/o/r.git"
    assert out["d"] == "<object>" and out["n"] == 3 and out["ok"] is True and out["none"] is None


# --- untraced by default ----------------------------------------------------------------

def test_disabled_run_executes_without_any_trace(world, work, logger):
    ended = []
    ctx = ToolContext(repo_root=work, test_python=sys.executable, install_deps=False, approve=lambda p: True)
    result = run_traced("graph", run_graph, ScriptedModel(FIX), ctx, Limits(), logger, target="t", on_end=ended.append)
    assert result.stop_reason is StopReason.DONE and ended == []


# --- one trace per run, on both engines -------------------------------------------------

@pytest.mark.parametrize("engine, drive", [("loop", run), ("graph", run_graph)])
def test_one_trace_per_run_with_metadata_spans_and_no_secrets(engine, drive, world, work, logger, client, monkeypatch):
    monkeypatch.setenv("GITHUB_TOKEN", FAKE_GH)
    monkeypatch.setenv("OPENAI_API_KEY", FAKE_OPENAI)
    world.fixed_version = "2.31.0"
    ctx = ToolContext(repo_root=work, test_python=sys.executable, install_deps=False)
    ctx.approve = Checkpoint(ctx, logger, ask=ScriptedHuman(["y"]), show=lambda s: None)
    # untrusted text that happens to contain a token reaches a model message and a tool result
    model = ScriptedModel(FIX[:1] + [("read_file", {"path": f"requirements.txt {FAKE_GH}"})] + FIX[1:])
    ended = []

    ls_client, session = client
    result = run_traced(engine, drive, model, ctx, Limits(), logger, target="https://u:pw@github.com/o/r",
                        client=ls_client, on_end=ended.append)

    assert result.stop_reason is StopReason.DONE
    assert len(ended) == 1  # one trace per run
    root = ended[0]
    assert root.name == "patchpilot_run"
    meta = root.metadata
    assert (meta["run_id"], meta["engine"], meta["model"], meta["stop_reason"]) == (logger.run_id, engine, "scripted", "done")
    assert meta["target"] == f"https://{REDACTED}@github.com/o/r"

    runs = list(walk(root))
    names = [r.name for r in runs]
    assert names.count("model_call") == result.state.steps == len(tool_records(logger))
    assert names.count("tool_call") == len(tool_records(logger))
    assert names.count("checkpoint_decision") == 1
    decision = next(r for r in runs if r.name == "checkpoint_decision")
    assert decision.outputs == {"approved": True} and "diff" in decision.inputs["proposal"]
    tool = next(r for r in runs if r.name == "tool_call")
    assert set(tool.inputs) == {"tool", "args", "step"} and tool.outputs["ok"] is True

    # what would have left the machine: every request body the client sent
    sent = "\n".join(body or "" for _, _, body in session.calls)
    assert session.calls and all("langsmith.invalid" in url for _, url, _ in session.calls)
    assert "patchpilot_run" in sent and "model_call" in sent and "tool_call" in sent
    assert FAKE_GH not in sent and FAKE_OPENAI not in sent and "u:pw" not in sent
    assert REDACTED in sent
    assert "repo_root" not in sent and "trivy_cache" not in sent  # the ToolContext is never traced
    if engine == "graph":
        assert '"stop_reason"' in sent  # LangGraph's own node runs were sent, and redacted too
