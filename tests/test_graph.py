"""The LangGraph engine: same steps, same stop rule, same RunResult as the loop.

Cross-engine parity over the key scenarios is in tests/test_parity.py.
"""

from agent import graph
from agent.graph import build_graph, draw_mermaid, run_graph
from agent.loop import start
from agent.model import ModelReply, ScriptedModel
from agent.stop_rule import Limits, StopReason
from tests.conftest import PROJECT_ROOT
from tests.test_loop import FIX, logger, make_ctx, tool_records, work, world  # noqa: F401  (fixtures)


def test_fix_ends_done_with_the_same_steps_and_a_commit(world, make_ctx, logger):
    result = run_graph(ScriptedModel(FIX), make_ctx(True), logger=logger)
    assert result.stop_reason is StopReason.DONE and result.state.steps == 5 and result.commit
    assert [r["tool"] for r in tool_records(logger)] == [t for t, _ in FIX]
    events = [r.get("event") for r in logger.read() if r.get("event")]
    assert events[0] == "start" and events[-1] == "stop" and events.count("model") == 5


def test_graph_state_carries_the_named_stop_reason(world, make_ctx, logger):
    s = start(ScriptedModel(FIX), make_ctx(True), Limits(), logger)
    g, stop = build_graph(s)
    out = g.invoke({"messages": s.messages, "run_state": s.state, "stop_reason": None})
    assert out["stop_reason"] == "done" and out["run_state"] is s.state
    assert stop["result"].stop_reason is StopReason.DONE


def test_a_model_that_never_finishes_halts_at_twelve_steps(world, make_ctx, logger):
    model = ScriptedModel([("read_file", {"path": "requirements.txt"})])  # forever
    result = run_graph(model, make_ctx(True), logger=logger)
    assert result.stop_reason is StopReason.STEP_LIMIT
    assert result.state.steps == 12 and model.calls == 12 and len(tool_records(logger)) == 12


def test_turns_without_tool_calls_still_count(world, make_ctx, logger):
    model = ScriptedModel([ModelReply(text="thinking...")])
    result = run_graph(model, make_ctx(True), logger=logger)
    assert result.stop_reason is StopReason.STEP_LIMIT and result.state.steps == 12
    assert tool_records(logger) == []


def test_recursion_limit_is_only_a_backstop(world, make_ctx, logger, monkeypatch):
    assert graph.recursion_limit(Limits()) > 2 * Limits().max_steps + 1  # never reached by a normal run
    monkeypatch.setattr(graph, "recursion_limit", lambda limits: 3)  # force the backstop
    model = ScriptedModel([("read_file", {"path": "requirements.txt"})])
    result = run_graph(model, make_ctx(True), logger=logger)
    assert result.stop_reason is StopReason.STEP_LIMIT
    assert any(r.get("event") == "backstop" for r in logger.read())
    assert logger.read()[-1]["event"] == "stop"


def test_docs_drawing_is_current():
    assert draw_mermaid().strip() in (PROJECT_ROOT / "docs" / "graph.md").read_text()
