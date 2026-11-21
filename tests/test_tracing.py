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
from tests.test_loop import FIX, logger, make_ctx, tool_records, work, world  # noqa: F401  (fixtures)

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
    tools = [r["tool"] for r in tool_records(logger)]
    assert names.count("model_call") == result.state.steps == len(tools)
    assert [r.name for r in runs if r.run_type == "tool"] == tools  # one span per tool call, named after it
    assert names.count("checkpoint_decision") == 1
    decision = next(r for r in runs if r.name == "checkpoint_decision")
    assert decision.outputs == {"approved": True} and "diff" in decision.inputs["proposal"]
    tool = next(r for r in runs if r.name == "run_tests")
    assert set(tool.inputs) == {"tool", "args", "step"} and tool.outputs["ok"] is True

    # what would have left the machine: every request body the client sent
    sent = "\n".join(body or "" for _, _, body in session.calls)
    assert session.calls and all("langsmith.invalid" in url for _, url, _ in session.calls)
    assert "patchpilot_run" in sent and "model_call" in sent and '"name":"run_scan"' in sent.replace(" ", "")
    assert FAKE_GH not in sent and FAKE_OPENAI not in sent and "u:pw" not in sent
    assert REDACTED in sent
    assert "repo_root" not in sent and "trivy_cache" not in sent  # the ToolContext is never traced
    if engine == "graph":
        assert '"stop_reason"' in sent  # LangGraph's own node runs were sent, and redacted too


# --- span tree shape, names, timing and model metadata (what LangSmith receives) --------

def sent_runs(session) -> dict:
    """Merge every POST/PATCH the client sent into one record per run id."""
    runs: dict = {}
    for method, url, body in session.calls:
        data = json.loads(body)
        r = runs.setdefault(data["id"], {})
        for key in ("name", "run_type", "parent_run_id", "trace_id", "start_time", "end_time"):
            if data.get(key) is not None:
                r[key] = data[key]
        meta = (data.get("extra") or {}).get("metadata")
        if meta:
            r.setdefault("metadata", {}).update(meta)
        if data.get("outputs") is not None:
            r["outputs"] = data["outputs"]
    return runs


def seconds(r) -> float:
    from datetime import datetime
    return (datetime.fromisoformat(r["end_time"]) - datetime.fromisoformat(r["start_time"])).total_seconds()


def ancestors(runs, r):
    while r.get("parent_run_id"):
        r = runs[r["parent_run_id"]]
        yield r


@pytest.mark.parametrize("engine, drive", [("loop", run), ("graph", run_graph)])
def test_span_tree_has_one_root_named_tools_real_timing_and_model_metadata(
        engine, drive, world, work, logger, client, monkeypatch):
    import time
    from agent.rules_model import RulesModel
    from agent.tools import registry
    slow = registry.TOOLS["run_tests"]
    monkeypatch.setitem(registry.TOOLS, "run_tests",
                        slow.__class__(slow.name, slow.description, slow.input_schema,
                                       lambda a, c, run=slow.run: (time.sleep(0.05), run(a, c))[1], slow.writes))
    ctx = ToolContext(repo_root=work, test_python=sys.executable, install_deps=False)
    ctx.approve = Checkpoint(ctx, logger, ask=ScriptedHuman(["y"]), show=lambda s: None)
    ls_client, session = client

    result = run_traced(engine, drive, RulesModel(), ctx, Limits(), logger, target="t", client=ls_client)
    assert result.stop_reason is StopReason.DONE
    runs = sent_runs(session)

    # 1. exactly one root, patchpilot_run; everything else nests under it, in the same trace
    roots = [r for r in runs.values() if not r.get("parent_run_id")]
    assert [r["name"] for r in roots] == ["patchpilot_run"]
    root_id = next(i for i, r in runs.items() if r is roots[0])
    assert {r["trace_id"] for r in runs.values()} == {root_id}
    assert all(any(a is roots[0] for a in ancestors(runs, r)) for r in runs.values() if r is not roots[0])
    if engine == "graph":
        assert any(r["name"] == "LangGraph" for r in runs.values())  # LangGraph's own runs nest too

    # 2. one span per tool call, named after the tool, run_type "tool"
    tool_spans = sorted((r for r in runs.values() if r.get("run_type") == "tool"), key=lambda r: r["start_time"])
    assert [r["name"] for r in tool_spans] == ["run_scan", "read_file", "edit_file", "run_tests", "run_scan"]
    assert not any(r["name"] == "tool_call" for r in runs.values())
    decision = next(r for r in runs.values() if r["name"] == "checkpoint_decision")
    assert runs[decision["parent_run_id"]]["name"] == "edit_file"  # the approval sits inside its write

    # 3. real start/end times on every span: the 50 ms test run shows up as >= 50 ms
    assert all(r.get("start_time") and r.get("end_time") and seconds(r) >= 0 for r in runs.values())
    assert seconds(next(r for r in tool_spans if r["name"] == "run_tests")) >= 0.05
    assert seconds(roots[0]) >= 0.05 and seconds(roots[0]) >= max(seconds(r) for r in runs.values())

    # 4. model spans: our rules model is filed as patchpilot/rules, as a chain step (no LLM call)
    model_spans = [r for r in runs.values() if r["name"] == "model_call"]
    assert len(model_spans) == 5
    for r in model_spans:
        assert r["run_type"] == "chain"
        assert (r["metadata"]["ls_provider"], r["metadata"]["ls_model_name"]) == ("patchpilot", "rules")


def test_llm_calls_are_llm_spans_with_provider_model_and_usage(world, make_ctx, logger, client):
    from tests.test_lc_model import call, fake
    from agent.lc_model import LangChainModel
    chat = fake(call("run_scan", {}, "c1"), call("read_file", {"path": "requirements.txt"}, "c2"),
                call("edit_file", {"path": "requirements.txt", "package": "requests", "version": "2.31.0"}, "c3"),
                call("run_tests", {}, "c4"), call("run_scan", {}, "c5"))
    ls_client, session = client
    result = run_traced("graph", run_graph, LangChainModel("gpt-4o-mini", chat=chat), make_ctx(True), Limits(),
                        logger, target="t", client=ls_client)
    assert result.stop_reason is StopReason.DONE
    runs = sent_runs(session)
    model_spans = [r for r in runs.values() if r["name"] == "model_call"]
    assert len(model_spans) == 5
    for r in model_spans:
        assert r["run_type"] == "llm"
        assert (r["metadata"]["ls_provider"], r["metadata"]["ls_model_name"]) == ("openai", "gpt-4o-mini")
        assert r["outputs"]["usage_metadata"] == {"input_tokens": 100, "output_tokens": 20, "total_tokens": 120}
    # LangChain's own chat-model run nests inside our model span, not as a separate trace
    chat_runs = [r for r in runs.values() if r["name"] == "FakeChat"]
    assert len(chat_runs) == 5 and all(runs[r["parent_run_id"]]["name"] == "model_call" for r in chat_runs)
    assert len([r for r in runs.values() if not r.get("parent_run_id")]) == 1


def test_every_tool_span_is_named_after_its_tool(client, ctx):
    from agent.tools.registry import TOOLS, call_tool
    from langsmith import traceable, tracing_context
    ls_client, session = client

    @traceable(name="patchpilot_run")
    def root():
        for name in TOOLS:
            call_tool(name, {"unexpected": 1}, ctx)  # refused by the strict schema: fast, no side effects

    with tracing_context(enabled=True, client=ls_client):
        root()
    runs = sent_runs(session)
    spans = sorted((r for r in runs.values() if r.get("run_type") == "tool"), key=lambda r: r["start_time"])
    assert [r["name"] for r in spans] == ["run_scan", "read_file", "run_tests", "edit_file", "create_issue",
                                          "open_pr", "merge_pr"]


def test_spans_outside_a_run_never_start_a_trace(client, ctx, world):
    from agent.tools.registry import call_tool
    from langsmith import tracing_context
    ls_client, session = client
    with tracing_context(enabled=True, client=ls_client):
        res = call_tool("read_file", {"path": "requirements.txt"}, ctx)
        decided = Checkpoint(ctx, None, ask=ScriptedHuman(["n"]), show=lambda s: None)(
            {"tool": "create_issue", "mode": "local", "title": "t", "body": "b"})
    assert res.ok and decided is False  # behavior unchanged
    assert session.calls == []  # no orphan root traces
