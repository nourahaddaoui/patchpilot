"""LangChainModel with a fake LangChain chat model: no network, no key."""

import json

import pytest
from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage

from agent.lc_model import LOCAL_SERVER_KEY, LangChainModel, to_langchain, to_reply
from agent.loop import run
from agent.stop_rule import StopReason
from agent.tools.registry import tool_definitions
from tests.test_loop import logger, make_ctx, tool_records, work, world  # noqa: F401  (fixtures)


class FakeChat(GenericFakeChatModel):
    """GenericFakeChatModel plus bind_tools (records what was bound) and the inputs it saw."""

    bound: list = []
    seen: list = []

    def bind_tools(self, tools, **kwargs):
        self.bound.append({"tools": tools, **kwargs})
        return self

    def invoke(self, input, *args, **kwargs):
        self.seen.append(list(input))
        return super().invoke(input, *args, **kwargs)


def fake(*messages):
    chat = FakeChat(messages=iter(messages))
    chat.bound, chat.seen = [], []
    return chat


def call(name, args, cid="c1", tin=100, tout=20):
    return AIMessage(content="", tool_calls=[{"name": name, "args": args, "id": cid}],
                     usage_metadata={"input_tokens": tin, "output_tokens": tout, "total_tokens": tin + tout})


def test_tool_call_becomes_a_model_reply_with_cost():
    chat = fake(call("edit_file", {"path": "requirements.txt", "package": "requests", "version": "2.31.0"}))
    reply = LangChainModel("gpt-4o-mini", chat=chat).complete([{"role": "user", "content": "fix"}], tool_definitions())
    assert [(c.id, c.name) for c in reply.tool_calls] == [("c1", "edit_file")]
    assert json.loads(reply.tool_calls[0].arguments) == {"path": "requirements.txt", "package": "requests", "version": "2.31.0"}
    assert (reply.input_tokens, reply.output_tokens) == (100, 20)
    assert reply.cost_usd == pytest.approx((100 * 0.15 + 20 * 0.60) / 1_000_000)


def test_tools_bound_once_as_openai_functions_without_parallel_calls():
    chat = fake(call("run_scan", {}), call("run_scan", {}, "c2"))
    model = LangChainModel("gpt-4o-mini", chat=chat)
    tools = tool_definitions()
    model.complete([{"role": "user", "content": "x"}], tools)
    model.complete([{"role": "user", "content": "x"}], tools)
    assert len(chat.bound) == 1 and chat.bound[0]["parallel_tool_calls"] is False
    names = [t["function"]["name"] for t in chat.bound[0]["tools"]]
    assert names == [t["name"] for t in tools]
    assert all(t["function"]["parameters"]["additionalProperties"] is False for t in chat.bound[0]["tools"])


def test_text_reply_has_no_tool_calls():
    reply = to_reply(AIMessage(content="done"), 0.15, 0.6)
    assert reply.text == "done" and reply.tool_calls == [] and reply.cost_usd == 0


def test_messages_are_converted_both_ways():
    messages = [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "task"},
        {"role": "assistant", "content": None, "tool_calls": [
            {"id": "c1", "type": "function", "function": {"name": "read_file", "arguments": '{"path": "requirements.txt"}'}}]},
        {"role": "tool", "tool_call_id": "c1", "content": "requests==2.25.1"},
        {"role": "assistant", "content": None, "tool_calls": [
            {"id": "c2", "type": "function", "function": {"name": "run_scan", "arguments": "not json"}}]},
    ]
    lc = to_langchain(messages)
    assert [type(m) for m in lc] == [SystemMessage, HumanMessage, AIMessage, ToolMessage, AIMessage]
    assert lc[2].tool_calls[0]["args"] == {"path": "requirements.txt"} and lc[2].tool_calls[0]["id"] == "c1"
    assert lc[3].tool_call_id == "c1"
    assert lc[4].tool_calls == [] and lc[4].invalid_tool_calls[0]["args"] == "not json"


def test_invalid_arguments_reach_the_registry_as_raw_text():
    msg = AIMessage(content="", invalid_tool_calls=[{"name": "edit_file", "args": "{oops", "id": "b1", "error": "x"}])
    reply = to_reply(msg, 0, 0)
    assert [(c.name, c.arguments) for c in reply.tool_calls] == [("edit_file", "{oops")]


def test_unknown_model_price_is_refused(monkeypatch):
    monkeypatch.delenv("PATCHPILOT_PRICE_IN", raising=False)
    monkeypatch.delenv("PATCHPILOT_PRICE_OUT", raising=False)
    with pytest.raises(ValueError, match="cost cap"):
        LangChainModel("some-new-model", chat=fake())


def test_missing_key_without_local_server_is_a_clear_error():
    with pytest.raises(RuntimeError, match="OPENAI_API_KEY"):
        LangChainModel("gpt-4o-mini")


def test_openai_base_url_points_chatopenai_at_a_local_server(monkeypatch):
    monkeypatch.setenv("OPENAI_BASE_URL", "http://localhost:11434/v1")
    monkeypatch.setenv("PATCHPILOT_PRICE_IN", "0")
    monkeypatch.setenv("PATCHPILOT_PRICE_OUT", "0")
    model = LangChainModel("llama3.1")  # builds ChatOpenAI; no request is made
    assert model.chat.openai_api_base == "http://localhost:11434/v1"
    assert model.chat.model_name == "llama3.1"
    uses_placeholder = model.chat.openai_api_key.get_secret_value() == LOCAL_SERVER_KEY  # never print a key
    assert uses_placeholder, "a real key was used"
    assert (model.price_in, model.price_out) == (0.0, 0.0)


def test_drives_the_loop_to_done(world, make_ctx, logger):
    chat = fake(call("run_scan", {}, "c1"), call("read_file", {"path": "requirements.txt"}, "c2"),
                call("edit_file", {"path": "requirements.txt", "package": "requests", "version": "2.31.0"}, "c3"),
                call("run_tests", {}, "c4"), call("run_scan", {}, "c5"))
    result = run(LangChainModel("gpt-4o-mini", chat=chat), make_ctx(True), logger=logger)
    assert result.stop_reason is StopReason.DONE
    assert [r["tool"] for r in tool_records(logger)] == ["run_scan", "read_file", "edit_file", "run_tests", "run_scan"]
    assert result.state.cost_usd == pytest.approx(5 * (100 * 0.15 + 20 * 0.60) / 1_000_000)
    # the model saw the tool results it asked for, linked by id
    last_input = chat.seen[-1]
    assert isinstance(last_input[-1], ToolMessage) and last_input[-1].tool_call_id == "c4"
