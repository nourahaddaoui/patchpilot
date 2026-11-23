"""LangChainModel: the LLM through LangChain, behind our own Model interface.

LangChain only talks to the LLM and binds the tools. Everything else stays ours:
the loop or graph decides when to call complete(), the stop rule decides when to stop,
the registry validates and runs the tool the model asked for, and the checkpoint asks
the human. complete() takes and returns exactly what OpenAIModel does (OpenAI-format
messages in, ModelReply out), so both engines and every test treat it the same.

Safety rules:
- One tool call per step: the tools are bound with parallel_tool_calls=False, and the
  engine executes only the first call anyway.
- The cost cap must work: the price comes from price_for(); a model without a known
  price is refused, as for OpenAIModel. Cost is counted from usage_metadata per call.
- Arguments are passed back as raw JSON text; the registry validates them, never LangChain.
- OPENAI_BASE_URL points it at any OpenAI-compatible server (e.g. Ollama). Only then may
  OPENAI_API_KEY be missing; a placeholder (never a real key) is sent to the local server.
"""

from __future__ import annotations

import json
import os

from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, SystemMessage, ToolMessage

from agent.injection import enabled as injection_defense_enabled, guard
from agent.model import DEFAULT_MODEL, ModelReply, ToolCall, load_dotenv, openai_tools, price_for

LOCAL_SERVER_KEY = "not-needed-for-a-local-server"


def to_langchain(messages: list[dict]) -> list[BaseMessage]:
    """OpenAI-format messages (what the engines keep) -> LangChain messages."""
    out: list[BaseMessage] = []
    for m in messages:
        role, content = m.get("role"), m.get("content") or ""
        if role == "system":
            out.append(SystemMessage(content=content))
        elif role == "user":
            out.append(HumanMessage(content=content))
        elif role == "tool":
            out.append(ToolMessage(content=content, tool_call_id=m.get("tool_call_id") or ""))
        elif role == "assistant":
            calls, invalid = [], []
            for c in m.get("tool_calls") or []:
                fn = c.get("function") or {}
                raw = fn.get("arguments") or ""
                try:
                    args = json.loads(raw) if raw.strip() else {}
                except json.JSONDecodeError:
                    args = None
                if isinstance(args, dict):
                    calls.append({"name": fn.get("name"), "args": args, "id": c.get("id"), "type": "tool_call"})
                else:  # keep what the model really sent, so the history stays truthful
                    invalid.append({"name": fn.get("name"), "args": raw, "id": c.get("id"),
                                    "error": "arguments are not a JSON object", "type": "invalid_tool_call"})
            out.append(AIMessage(content=content, tool_calls=calls, invalid_tool_calls=invalid))
    return out


def to_reply(msg: AIMessage, price_in: float, price_out: float) -> ModelReply:
    """LangChain AIMessage -> ModelReply (tool calls with raw JSON arguments, tokens, cost)."""
    calls = [ToolCall(c.get("id") or f"lc_{i}", c["name"], json.dumps(c.get("args") or {}))
             for i, c in enumerate(msg.tool_calls or [])]
    calls += [ToolCall(c.get("id") or f"lc_bad_{i}", c.get("name") or "", c.get("args") or "")
              for i, c in enumerate(getattr(msg, "invalid_tool_calls", None) or [])]
    usage = msg.usage_metadata or {}
    tin, tout = int(usage.get("input_tokens") or 0), int(usage.get("output_tokens") or 0)
    text = msg.content if isinstance(msg.content, str) else json.dumps(msg.content)
    return ModelReply(text or None, calls, tin, tout, (tin * price_in + tout * price_out) / 1_000_000)


def _chat_openai(name: str):
    from langchain_openai import ChatOpenAI

    base_url = os.environ.get("OPENAI_BASE_URL") or None
    key = os.environ.get("OPENAI_API_KEY")
    if not key and not base_url:
        raise RuntimeError("OPENAI_API_KEY is not set. Put it in your environment or in a local .env file "
                           "(never commit it), or set OPENAI_BASE_URL to a local OpenAI-compatible server.")
    return ChatOpenAI(model=name, base_url=base_url, api_key=key or LOCAL_SERVER_KEY)


class LangChainModel:
    """Model interface over any LangChain chat model. Default: ChatOpenAI(model=name)."""

    def __init__(self, model: str | None = None, chat=None):
        load_dotenv()
        self.name = model or os.environ.get("PATCHPILOT_MODEL", DEFAULT_MODEL)
        self.price_in, self.price_out = price_for(self.name)  # before any client: unknown price = refused
        self.chat = chat if chat is not None else _chat_openai(self.name)
        self.defense = injection_defense_enabled()  # agent/injection.py; the eval turns it off for "before"
        self._bound = None
        self._bound_for: list[dict] | None = None

    def _bind(self, tools: list[dict]):
        if self._bound is None or self._bound_for != tools:
            self._bound = self.chat.bind_tools(openai_tools(tools), parallel_tool_calls=False)
            self._bound_for = tools
        return self._bound

    def complete(self, messages: list[dict], tools: list[dict]) -> ModelReply:
        msg = self._bind(tools).invoke(to_langchain(guard(messages, self.defense)))  # tool output = data
        return to_reply(msg, self.price_in, self.price_out)
