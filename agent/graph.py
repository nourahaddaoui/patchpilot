"""The LangGraph engine: the same run as agent/loop.py, driven by a StateGraph.

    START --gate_model--> model --gate_no_tool--> nudge --gate_model--> model ...
                                --gate_tool----> tools --gate_model--> model ...
    any gate that returns a stop reason --> finish --> END
    (drawing: docs/graph.md)

LangGraph only sequences the steps. Every step is a shared function from
agent/loop.py, so the safety proof does not move into the framework:

- Conditional edges call OUR stop rule through gate_model / gate_no_tool / gate_tool,
  exactly where the loop calls it: before every model call and before every tool call.
  A reason routes to "finish", which records it as the graph's stop_reason and ends.
- "tools" runs tool_turn: registry validation, one retry for transient read failures,
  observe(), rollback after red tests, the JSONL record. Human approval stays inside the
  write tools (ctx.approve), so no write can happen without a recorded yes.
- "finish" runs finish(): cleanup of untested edits, the commit on DONE, the stop record.
- The 12-step limit is enforced by check(), as in the loop. recursion_limit is only a
  backstop above it (2 graph steps per agent step, plus finish, plus margin); if it ever
  fires, the run still ends through finish() as step_limit and the event is logged.
"""

from __future__ import annotations

from typing import Optional, TypedDict

from langgraph.errors import GraphRecursionError
from langgraph.graph import END, START, StateGraph

from agent.calllog import CallLogger
from agent.loop import (
    TASK, RunResult, Session, finish, gate_model, gate_no_tool, gate_tool, model_turn, nudge, start, tool_turn,
)
from agent.model import Model
from agent.stop_rule import Limits, RunState, StopReason
from agent.tools.context import ToolContext


class GraphState(TypedDict):
    messages: list[dict]  # the conversation, OpenAI format (the same list the Session holds)
    run_state: RunState  # our stop-rule state (the same object the Session holds)
    stop_reason: Optional[str]  # set once, by "finish"


def recursion_limit(limits: Limits) -> int:
    """Backstop only: check() stops at max_steps long before this (2 graph steps per agent step)."""
    return 2 * limits.max_steps + 5


def build_graph(s: Session | None, commit: bool = True):
    """Compile the graph for one Session; returns (graph, stop). (s=None compiles it only to draw it.)"""
    stop: dict = {}  # the reason a gate returned, handed to "finish"

    def _view() -> dict:
        return {"messages": s.messages, "run_state": s.state}

    def to_finish(reason: StopReason) -> str:
        stop["reason"] = reason
        return "finish"

    def route_before_model(state: GraphState) -> str:
        reason = gate_model(s)
        return to_finish(reason) if reason else "model"

    def route_after_model(state: GraphState) -> str:
        if not s.reply.tool_calls:
            reason = gate_no_tool(s)
            return to_finish(reason) if reason else "nudge"
        reason = gate_tool(s, s.reply.tool_calls[0])  # one tool call per step; extras are never executed
        return to_finish(reason) if reason else "tools"

    def model_node(state: GraphState) -> dict:
        model_turn(s)
        return _view()

    def nudge_node(state: GraphState) -> dict:
        nudge(s)
        return _view()

    def tools_node(state: GraphState) -> dict:
        tool_turn(s, s.reply.tool_calls[0])
        return _view()

    def finish_node(state: GraphState) -> dict:
        stop["result"] = finish(s, stop["reason"], commit)
        return {**_view(), "stop_reason": stop["reason"].value}

    g = StateGraph(GraphState)
    g.add_node("model", model_node)
    g.add_node("nudge", nudge_node)
    g.add_node("tools", tools_node)
    g.add_node("finish", finish_node)
    to_model = {"model": "model", "finish": "finish"}
    g.add_conditional_edges(START, route_before_model, to_model)
    g.add_conditional_edges("model", route_after_model, {"nudge": "nudge", "tools": "tools", "finish": "finish"})
    g.add_conditional_edges("nudge", route_before_model, to_model)
    g.add_conditional_edges("tools", route_before_model, to_model)
    g.add_edge("finish", END)
    return g.compile(), stop  # stop["result"] is where run_graph picks up the RunResult


def draw_mermaid() -> str:
    return build_graph(None)[0].get_graph().draw_mermaid()


def run_graph(
    model: Model,
    ctx: ToolContext,
    limits: Limits = Limits(),
    logger: CallLogger | None = None,
    task: str = TASK,
    commit: bool = True,
) -> RunResult:
    """Same contract as agent.loop.run: same inputs, same RunResult, same JSONL log."""
    s = start(model, ctx, limits, logger, task)
    graph, stop = build_graph(s, commit)
    try:
        graph.invoke({"messages": s.messages, "run_state": s.state, "stop_reason": None},
                     {"recursion_limit": recursion_limit(limits)})
    except GraphRecursionError:
        s.logger.log({"event": "backstop", "what": "recursion_limit", "steps": s.state.steps})
        return finish(s, StopReason.STEP_LIMIT, commit)
    return stop["result"]
