"""Web runs: the real agent on one eval scenario, with the human checkpoint in the browser.

A run is exactly what `python run.py eval` does for one scenario (eval/runner.py: the
template repo plus the scenario's files, a real sandbox, the simulated trivy/pip/pytest
and GitHub, the real engine, tools, checkpoint, recovery and stop rule), with one
difference: the human is whoever is looking at the page.

BrowserHuman is what the Checkpoint calls. show() keeps the text the checkpoint
renders (the diff, the scan before and after, or the issue / PR text); ask() publishes
the question and waits for an answer from the browser. No answer within
ANSWER_TIMEOUT_S counts as "no", like closed input in the terminal: only an explicit
yes ever approves a write.

Only scenario ids from eval/scenarios.py can be run. Nobody can point the hosted demo
at a real repository, so no stranger's code ever runs on the server.
"""

from __future__ import annotations

import os
import queue
import shutil
import sys
import tempfile
import threading
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path

from langsmith import tracing_context

from agent.calllog import CallLogger
from agent.checkpoint import Checkpoint
from agent.tools.context import ToolContext
from eval import grade
from eval.fake_github import REPO, FakeGitHub
from eval.runner import ENGINES, build_repo, make_model
from eval.scenarios import BY_ID, Scenario
from eval.world import FakeWorld

ANSWER_TIMEOUT_S = float(os.environ.get("PATCHPILOT_WEB_ANSWER_TIMEOUT_S", "300"))
MAX_ACTIVE = int(os.environ.get("PATCHPILOT_WEB_MAX_RUNS", "3"))
KEEP_FINISHED_S = 30 * 60
FREE_MODELS = ("rules", "compromised")


class RunRefused(Exception):
    """The request cannot start a run (unknown scenario, model not allowed, server busy)."""


def llm_models() -> tuple[str, ...]:
    """Paid models the server owner switched on (PATCHPILOT_WEB_LLM_MODELS=gpt-4o-mini,...)."""
    return tuple(m.strip() for m in os.environ.get("PATCHPILOT_WEB_LLM_MODELS", "").split(",") if m.strip())


def models_for(scn: Scenario) -> list[str]:
    if scn.model is not None:
        return ["scripted"]  # limits scenarios bring their own deliberately broken model
    return ["rules"] + (["compromised"] if scn.attack is not None else []) + list(llm_models())


class BrowserHuman:
    """The person at the page, as seen by the Checkpoint (its `ask` and `show`)."""

    def __init__(self, timeout_s: float = ANSWER_TIMEOUT_S):
        self.timeout_s = timeout_s
        self._answers: queue.Queue[str] = queue.Queue()
        self._lock = threading.Lock()
        self._shown = ""
        self.pending: dict | None = None  # {"question", "text", "since"} while waiting
        self.decisions: list[dict] = []
        self.closed = False

    def show(self, text: str) -> None:
        with self._lock:
            if self.pending is None:
                self._shown = text  # the proposal; replies after a decision are not questions

    def ask(self, question: str) -> str:
        with self._lock:
            if self.closed:
                raise EOFError
            self.pending = {"question": question.strip(), "text": self._shown, "since": time.time()}
        try:
            answer = self._answers.get(timeout=self.timeout_s)
        except queue.Empty:
            answer = None
        with self._lock:
            self.decisions.append({"question": question.strip(), "answer": answer or "<no answer in time>"})
            self.pending = None
            self._shown = ""
            while not self._answers.empty():  # nothing may be left over for the next question
                self._answers.get_nowait()
        if answer is None:
            raise EOFError  # the Checkpoint reads EOF as "no"
        return answer

    def answer(self, approve: bool) -> bool:
        """Deliver the browser's answer to the question now on screen. False if none is waiting.

        One answer per question: the question is taken down as the answer is delivered, so a
        double click (or a second tab) can never carry a "yes" over to the next question."""
        with self._lock:
            if self.pending is None:
                return False
            self.pending = None
            self._answers.put("y" if approve else "n")
        return True

    def close(self) -> None:
        with self._lock:
            self.closed = True
        self._answers.put("n")


_token_lock = threading.Lock()
_token_users = 0


@contextmanager
def demo_github_token(on: bool):
    """GitHub-mode scenarios talk only to FakeGitHub, but the tools still want a token for the header.
    Reference-counted, so overlapping runs never remove it from under each other; a real token, if the
    server has one, is never replaced (and never used: every request goes to FakeGitHub)."""
    global _token_users
    if not on:
        yield
        return
    with _token_lock:
        if _token_users == 0 and not os.environ.get("GITHUB_TOKEN"):
            os.environ["GITHUB_TOKEN"] = "web-demo-fake-token"
        _token_users += 1
    try:
        yield
    finally:
        with _token_lock:
            _token_users -= 1
            if _token_users == 0 and os.environ.get("GITHUB_TOKEN") == "web-demo-fake-token":
                del os.environ["GITHUB_TOKEN"]


@dataclass
class WebRun:
    id: str
    scenario: Scenario
    model: str
    engine: str
    human: BrowserHuman
    status: str = "starting"  # starting | running | waiting | finished | error
    started: float = field(default_factory=time.time)
    finished: float | None = None
    log_path: Path | None = None
    result: dict | None = None
    error: str | None = None
    tmp: str | None = None
    thread: threading.Thread | None = None

    def records(self) -> list[dict]:
        if self.log_path is None or not self.log_path.exists():
            return []
        return CallLogger(run_id=self.log_path.stem, log_dir=self.log_path.parent).read()

    def view(self) -> dict:
        status = self.status
        if status == "running" and self.human.pending is not None:
            status = "waiting"
        return {"id": self.id, "scenario": self.scenario.id, "category": self.scenario.category,
                "description": self.scenario.description, "model": self.model, "engine": self.engine,
                "status": status, "pending": self.human.pending, "timeline": timeline(self.records()),
                "result": self.result, "error": self.error,
                "elapsed_s": round((self.finished or time.time()) - self.started, 1)}


def _short(text, limit: int = 600) -> str:
    text = "" if text is None else str(text)
    return text if len(text) <= limit else text[: limit - 1] + "…"


def timeline(records: list[dict]) -> list[dict]:
    """The JSONL trajectory as display rows (plain data; the page renders it as text, never as HTML)."""
    rows = []
    for r in records:
        event = r.get("event")
        if event == "model":
            calls = r.get("tool_calls") or []
            rows.append({"kind": "model", "step": r.get("step"), "text": _short(r.get("text"), 300),
                         "call": f"{calls[0]['name']}({_short(calls[0]['arguments'], 200)})" if calls else None})
        elif event is None and "tool" in r:
            rows.append({"kind": "tool", "step": r.get("step"), "tool": r["tool"], "ok": r.get("ok"),
                         "error_type": r.get("error_type"), "output": _short(r.get("output"))})
        elif event == "checkpoint":
            rows.append({"kind": "checkpoint", "approved": r.get("approved"), "answer": r.get("answer")})
        elif event == "recovery":
            rows.append({"kind": "recovery", "step": r.get("step"), "action": r.get("action"),
                         "tool": r.get("tool")})
        elif event == "stop":
            rows.append({"kind": "stop", "reason": r.get("reason"), "steps": r.get("steps"),
                         "cost_usd": r.get("cost_usd")})
        elif event == "eval_crash":
            rows.append({"kind": "error", "text": _short(r.get("error"))})
    return rows


class RunManager:
    def __init__(self, max_active: int = MAX_ACTIVE, answer_timeout_s: float = ANSWER_TIMEOUT_S):
        self.max_active = max_active
        self.answer_timeout_s = answer_timeout_s
        self.runs: dict[str, WebRun] = {}
        self._lock = threading.Lock()

    def active(self) -> int:
        return sum(1 for r in self.runs.values() if r.status in ("starting", "running"))

    def start(self, scenario_id: str, model: str = "rules", engine: str = "graph") -> WebRun:
        scn = BY_ID.get(scenario_id)
        if scn is None:
            raise RunRefused(f"unknown scenario '{scenario_id}'")
        if engine not in ENGINES:
            raise RunRefused(f"engine must be one of {sorted(ENGINES)}")
        allowed = models_for(scn)
        if scn.model is not None:
            model = "scripted"
        if model not in allowed:
            raise RunRefused(f"model '{model}' is not available for this scenario (allowed: {', '.join(allowed)})")
        with self._lock:
            self._expire()
            if self.active() >= self.max_active:
                raise RunRefused("the demo is busy; try again in a minute")
            run = WebRun(uuid.uuid4().hex[:12], scn, model, engine, BrowserHuman(self.answer_timeout_s))
            self.runs[run.id] = run
        run.thread = threading.Thread(target=self._execute, args=(run,), name=f"run-{run.id}", daemon=True)
        run.thread.start()
        return run

    def get(self, run_id: str) -> WebRun | None:
        return self.runs.get(run_id)

    def answer(self, run_id: str, approve: bool) -> bool:
        run = self.runs.get(run_id)
        return run is not None and run.human.answer(approve)

    def shutdown(self) -> None:
        for run in list(self.runs.values()):
            run.human.close()

    def _expire(self) -> None:
        now = time.time()
        for rid, run in list(self.runs.items()):
            if run.finished and now - run.finished > KEEP_FINISHED_S:
                if run.tmp:
                    shutil.rmtree(run.tmp, ignore_errors=True)
                del self.runs[rid]

    def _execute(self, run: WebRun) -> None:
        scn = run.scenario
        run.tmp = tempfile.mkdtemp(prefix="patchpilot-web-")
        try:
            with tracing_context(enabled=False):
                work = build_repo(scn, Path(run.tmp))
                world = FakeWorld(list(scn.advisories), fault=scn.fault, **scn.world)
                gh = FakeGitHub(scn.requirements, scn.ci_conclusion) if scn.github else None
                ctx = ToolContext(repo_root=work, test_python=sys.executable, install_deps=True, runner=world,
                                  trivy_cache=Path(run.tmp) / "trivy-cache",
                                  issue_mode="github" if scn.github else "local",
                                  github_repo=REPO if scn.github else None, http=gh, ci_poll_s=0, ci_timeout_s=5)
                logger = CallLogger(run_id=run.id, log_dir=Path(run.tmp) / "logs")
                run.log_path = logger.path
                ctx.approve = Checkpoint(ctx, logger, ask=run.human.ask, show=run.human.show)
                model = make_model("rules" if run.model == "scripted" else run.model, scn)
                run.status = "running"
                t0 = time.perf_counter()
                with demo_github_token(scn.github):
                    ENGINES[run.engine](model, ctx, logger=logger)
                elapsed = time.perf_counter() - t0
                final = (work / "requirements.txt").read_text(encoding="utf-8")
                row = grade.grade_run(scn, logger.read(), final, elapsed)
                run.result = {
                    "stop_reason": row["stop_reason"], "expected_stop": scn.expected_stop,
                    "steps": row["steps"], "success": row["success"],
                    "requirements_before": scn.requirements, "requirements_after": final,
                    "blocked_by": row.get("blocked_by"),
                }
            run.status = "finished"
        except Exception as e:  # shown on the page as a failed run; the server keeps going
            run.status, run.error = "error", f"{type(e).__name__}: {_short(e, 300)}"
        finally:
            run.finished = time.time()
            run.human.close()
