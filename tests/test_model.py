from types import SimpleNamespace as NS

import pytest

from agent.model import OpenAIModel, openai_tools, price_for
from agent.tools.registry import tool_definitions


class FakeCompletions:
    def __init__(self, response):
        self.response = response
        self.kwargs = None

    def create(self, **kwargs):
        self.kwargs = kwargs
        return self.response


def fake_client(response):
    completions = FakeCompletions(response)
    return NS(chat=NS(completions=completions)), completions


def response(content=None, tool_calls=None, prompt=1000, completion=200):
    msg = NS(content=content, tool_calls=tool_calls)
    return NS(choices=[NS(message=msg)], usage=NS(prompt_tokens=prompt, completion_tokens=completion))


def test_tool_call_reply_is_parsed_and_priced():
    call = NS(id="call_1", type="function", function=NS(name="run_scan", arguments="{}"))
    client, completions = fake_client(response(tool_calls=[call]))
    reply = OpenAIModel("gpt-4o-mini", client=client).complete([{"role": "user", "content": "hi"}], tool_definitions())

    assert [(c.id, c.name, c.arguments) for c in reply.tool_calls] == [("call_1", "run_scan", "{}")]
    assert (reply.input_tokens, reply.output_tokens) == (1000, 200)
    assert reply.cost_usd == pytest.approx((1000 * 0.15 + 200 * 0.60) / 1_000_000)
    assert completions.kwargs["parallel_tool_calls"] is False
    assert [t["function"]["name"] for t in completions.kwargs["tools"]] == ["run_scan", "read_file", "run_tests", "edit_file"]


def test_text_reply_has_no_tool_calls():
    client, _ = fake_client(response(content="done"))
    reply = OpenAIModel("gpt-4o-mini", client=client).complete([], [])
    assert reply.text == "done" and reply.tool_calls == []


def test_tools_keep_their_strict_schemas():
    for spec in openai_tools(tool_definitions()):
        assert spec["type"] == "function"
        assert spec["function"]["parameters"]["additionalProperties"] is False


def test_unknown_model_price_is_refused(monkeypatch):
    monkeypatch.delenv("PATCHPILOT_PRICE_IN", raising=False)
    monkeypatch.delenv("PATCHPILOT_PRICE_OUT", raising=False)
    with pytest.raises(ValueError, match="cost cap"):
        price_for("some-new-model")
    monkeypatch.setenv("PATCHPILOT_PRICE_IN", "1")
    monkeypatch.setenv("PATCHPILOT_PRICE_OUT", "2")
    assert price_for("some-new-model") == (1.0, 2.0)


def test_missing_api_key_is_a_clear_error(monkeypatch, tmp_path):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.setattr("agent.model.PROJECT_ROOT", tmp_path)  # no .env there
    monkeypatch.setattr("agent.model.load_dotenv", lambda *a, **k: None)
    with pytest.raises(RuntimeError, match="OPENAI_API_KEY"):
        OpenAIModel("gpt-4o-mini")
