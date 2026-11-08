"""The model behind the loop.

The loop only talks to the `Model` protocol, so tests (and the Day 17 stop-rule
proof) can swap in a scripted stub, and the real run uses OpenAI.

Messages use the OpenAI chat format:
    {"role": "system" | "user" | "assistant" | "tool", ...}
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

PROJECT_ROOT = Path(__file__).resolve().parents[1]

DEFAULT_MODEL = "gpt-4o-mini"

# USD per 1M tokens (input, output). Check https://openai.com/api/pricing before
# trusting the numbers; override with PATCHPILOT_PRICE_IN / PATCHPILOT_PRICE_OUT.
PRICES_PER_MTOK: dict[str, tuple[float, float]] = {
    "gpt-4o-mini": (0.15, 0.60),
    "gpt-4o": (2.50, 10.00),
    "gpt-4.1-mini": (0.40, 1.60),
    "gpt-4.1": (2.00, 8.00),
}


@dataclass(frozen=True)
class ToolCall:
    id: str
    name: str
    arguments: str  # raw JSON text exactly as the model produced it


@dataclass
class ModelReply:
    text: str | None
    tool_calls: list[ToolCall] = field(default_factory=list)
    input_tokens: int = 0
    output_tokens: int = 0
    cost_usd: float = 0.0


class Model(Protocol):
    name: str

    def complete(self, messages: list[dict], tools: list[dict]) -> ModelReply: ...


def openai_tools(tool_definitions: list[dict]) -> list[dict]:
    """Registry tool specs -> OpenAI function-tool specs."""
    return [
        {
            "type": "function",
            "function": {
                "name": t["name"],
                "description": t["description"],
                "parameters": t["input_schema"],
            },
        }
        for t in tool_definitions
    ]


def load_dotenv(path: Path = PROJECT_ROOT / ".env") -> None:
    """Minimal .env reader: KEY=VALUE lines, never overrides the real environment."""
    if not path.is_file():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def price_for(model: str) -> tuple[float, float]:
    env_in, env_out = os.environ.get("PATCHPILOT_PRICE_IN"), os.environ.get("PATCHPILOT_PRICE_OUT")
    if env_in and env_out:
        return float(env_in), float(env_out)
    if model not in PRICES_PER_MTOK:
        # An unknown price would make the cost cap silently never fire.
        raise ValueError(
            f"no price known for model '{model}'. Set PATCHPILOT_PRICE_IN and "
            "PATCHPILOT_PRICE_OUT (USD per 1M tokens) so the cost cap works."
        )
    return PRICES_PER_MTOK[model]


class OpenAIModel:
    def __init__(self, model: str | None = None, client=None):
        load_dotenv()
        self.name = model or os.environ.get("PATCHPILOT_MODEL", DEFAULT_MODEL)
        self.price_in, self.price_out = price_for(self.name)
        if client is None:
            if not os.environ.get("OPENAI_API_KEY"):
                raise RuntimeError(
                    "OPENAI_API_KEY is not set. Put it in your environment or in a local "
                    ".env file (never commit it)."
                )
            from openai import OpenAI

            client = OpenAI()
        self.client = client

    def complete(self, messages: list[dict], tools: list[dict]) -> ModelReply:
        response = self.client.chat.completions.create(
            model=self.name,
            messages=messages,
            tools=openai_tools(tools),
            tool_choice="auto",
            parallel_tool_calls=False,  # one tool call per step keeps the stop rule exact
        )
        msg = response.choices[0].message
        calls = [
            ToolCall(c.id, c.function.name, c.function.arguments or "")
            for c in (msg.tool_calls or [])
            if c.type == "function"
        ]
        usage = response.usage
        tin = usage.prompt_tokens if usage else 0
        tout = usage.completion_tokens if usage else 0
        cost = (tin * self.price_in + tout * self.price_out) / 1_000_000
        return ModelReply(msg.content, calls, tin, tout, cost)


class ScriptedModel:
    """Plays back a fixed list of replies; repeats the last one forever if asked for more.

    Each script item is a ModelReply, or a (tool_name, args_dict) shortcut.
    """

    name = "scripted"

    def __init__(self, script: list, cost_per_call: float = 0.0):
        self.script = list(script)
        self.cost_per_call = cost_per_call
        self.calls = 0
        self.seen: list[list[dict]] = []

    def complete(self, messages: list[dict], tools: list[dict]) -> ModelReply:
        self.seen.append(list(messages))
        item = self.script[min(self.calls, len(self.script) - 1)]
        self.calls += 1
        if isinstance(item, ModelReply):
            return item
        name, args = item
        return ModelReply(
            text=None,
            tool_calls=[ToolCall(f"call_{self.calls}", name, json.dumps(args))],
            input_tokens=100,
            output_tokens=20,
            cost_usd=self.cost_per_call,
        )
