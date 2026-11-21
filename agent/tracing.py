"""LangSmith tracing: opt-in, one trace per run, nothing secret in it.

Tracing observes; it never decides. The spans wrap our own functions (the run, each
model call, each tool call, each checkpoint decision) and change nothing they return.

Safety rules:
- Opt-in: a run is traced only when LANGSMITH_TRACING is true AND LANGSMITH_API_KEY is
  set (both read from the environment, never logged). Otherwise the whole run executes
  inside tracing_context(enabled=False), so neither our spans nor LangChain's own
  callbacks send anything.
- What a trace holds: prompts and model replies, tool names, inputs and outputs (which
  include repo file contents and scan results), checkpoint proposals and decisions, and
  the run metadata (run_id, engine, model, target, stop_reason).
- What it never holds: the ToolContext, the environment, or anything token-shaped.
  Redaction happens twice. Our spans pick their inputs/outputs explicitly (never ctx).
  And the LangSmith Client itself is built with hide_inputs/hide_outputs/hide_metadata =
  redact, the last point before anything leaves the machine, so the runs LangGraph and
  LangChain add on their own (graph nodes with the raw state, ChatOpenAI calls) are
  redacted too. redact() replaces the value of every secret environment variable (same
  rule as the env scrub: run_tests.is_secret), GitHub and OpenAI token patterns, Bearer
  headers and credentials inside URLs, and turns any other object into its type name.
"""

from __future__ import annotations

import functools
import inspect
import os
import re
from typing import Any, Callable

from langsmith import Client, get_current_run_tree, trace, traceable, tracing_context

from agent.tools.run_tests import is_secret

REDACTED = "[redacted]"
_TOKENS = re.compile(r"\b(?:gh[pousr]_|github_pat_|sk-)[A-Za-z0-9_\-]{8,}|Bearer\s+\S+")
_URL_CREDENTIALS = re.compile(r"(\w+://)[^/@\s]+@")
_MIN_SECRET_LEN = 6  # shorter values ("1", "true") are not worth matching and would mangle text


def enabled(env: dict | None = None) -> bool:
    """Tracing is on only with LANGSMITH_TRACING=true and a LANGSMITH_API_KEY."""
    env = os.environ if env is None else env
    return (env.get("LANGSMITH_TRACING", "").strip().lower() in ("true", "1", "yes")
            and bool(env.get("LANGSMITH_API_KEY", "").strip()))


def _secret_values() -> list[str]:
    values = {v for k, v in os.environ.items() if is_secret(k) and len(v) >= _MIN_SECRET_LEN}
    return sorted(values, key=len, reverse=True)


def redact(value: Any, secrets: list[str] | None = None) -> Any:
    """Copy of `value` (dicts, lists, strings, scalars) with every secret replaced."""
    secrets = _secret_values() if secrets is None else secrets
    if isinstance(value, str):
        for s in secrets:
            value = value.replace(s, REDACTED)
        value = _TOKENS.sub(REDACTED, value)
        return _URL_CREDENTIALS.sub(rf"\1{REDACTED}@", value)
    if isinstance(value, dict):
        return {str(k): redact(v, secrets) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [redact(v, secrets) for v in value]
    if value is None or isinstance(value, (bool, int, float)):
        return value
    return f"<{type(value).__name__}>"  # never serialize arbitrary objects (ctx, clients, loggers)


def span(name: str | Callable[[dict], str], run_type: str | Callable[[dict], str],
         inputs: Callable[[dict], dict], outputs: Callable[[Any], dict],
         metadata: Callable[[dict], dict] | None = None):
    """Decorator: trace one call as a child span of the current patchpilot_run.

    - Never a root: outside a traced run (no current run tree) the function is just called,
      so a span can never start its own trace.
    - name, run_type and metadata may be computed from the call's bound arguments (e.g. the
      tool span is named after the tool).
    - Real timing: the span starts when the call starts and ends when it returns or raises.
    - Only what `inputs` / `outputs` / `metadata` pick is traced, and it is redacted, so a
      new argument (e.g. a context object) is never traced by accident.
    """
    def decorate(fn):
        signature = inspect.signature(fn)

        @functools.wraps(fn)
        def wrapper(*args, **kwargs):
            parent = get_current_run_tree()
            if parent is None:
                return fn(*args, **kwargs)
            bound = signature.bind(*args, **kwargs)
            bound.apply_defaults()
            arguments = dict(bound.arguments)
            with trace(name(arguments) if callable(name) else name,
                       run_type=run_type(arguments) if callable(run_type) else run_type,
                       inputs=redact(inputs(arguments)),
                       metadata=redact(metadata(arguments)) if metadata else None,
                       parent=parent) as run:
                result = fn(*args, **kwargs)
                run.end(outputs=redact(outputs(result)))
                return result
        return wrapper
    return decorate


def model_metadata(model) -> dict:
    """ls_provider / ls_model_name, so LangSmith files the call under the right model.
    LLM classes declare provider = "openai"; our own models (rules, scripted) are "patchpilot"."""
    return {"ls_provider": getattr(model, "provider", "patchpilot"), "ls_model_name": model.name}


def model_run_type(model) -> str:
    """'llm' only for a real LLM call; our deterministic models are plain chain steps."""
    return "llm" if getattr(model, "provider", "patchpilot") != "patchpilot" else "chain"


def make_client(**kwargs) -> Client:
    """The only client PatchPilot traces through: every run it sends is redacted first.
    (Credentials come from LANGSMITH_API_KEY / LANGSMITH_ENDPOINT, read by the Client itself.)"""
    return Client(hide_inputs=redact, hide_outputs=redact, hide_metadata=redact, **kwargs)


def _run_outputs(result) -> dict:
    s = result.state
    return {"stop_reason": result.stop_reason.value, "steps": s.steps, "cost_usd": round(s.cost_usd, 6),
            "commit": result.commit, "scan_clean": s.scan_clean, "tests_green": s.tests_green}


def run_traced(engine: str, drive: Callable, model, ctx, limits, logger, target: str,
               client: Client | None = None, on_end: Callable | None = None):
    """Run one engine under one trace. Untraced (and nothing sent) unless enabled().
    Tests pass a make_client(...) built on a recording session instead of the network."""
    if client is None:
        if not enabled():
            with tracing_context(enabled=False):
                return drive(model, ctx, limits, logger)
        client = make_client()

    metadata = redact({"run_id": logger.run_id, "engine": engine, "model": model.name, "target": target})

    @traceable(name="patchpilot_run", run_type="chain",  # the one root of the trace; every span nests under it
               process_inputs=lambda d: metadata, process_outputs=lambda r: redact(_run_outputs(r)))
    def _run():
        result = drive(model, ctx, limits, logger)
        tree = get_current_run_tree()
        if tree is not None:
            tree.metadata["stop_reason"] = result.stop_reason.value
        return result

    extra = {"metadata": metadata, **({"on_end": on_end} if on_end else {})}
    try:
        with tracing_context(enabled=True, client=client, metadata=metadata):
            return _run(langsmith_extra=extra)
    finally:
        client.flush()
