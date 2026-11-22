"""Terminal UI (Textual): pick a repo, watch the run, approve or deny every write.

    python run.py --tui
    python run.py --tui --target https://github.com/OWNER/REPO --issues github

What you see:
  - a form: repo (GitHub URL, local folder, or empty for the template), model, engine,
    and whether issues are filed on GitHub (on that same repo) or locally;
  - a live counter: steps, cost, stop reason;
  - a step timeline, fed by the run's JSONL trajectory as it is written;
  - an approval panel: the exact diff / issue / PR the agent wants to write, and
    Approve / Deny buttons (or the y / n keys).

The safety logic is unchanged: the UI is only the `ask` and `show` functions of the
same Checkpoint the CLI uses. Only an explicit Approve is a yes; quitting while a
question is open answers no.
"""

from __future__ import annotations

import json
import queue
from typing import Callable

from rich.text import Text
from textual import work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.widgets import Button, Footer, Header, Input, Label, RichLog, Select, Static, Switch

from agent import __main__ as cli
from agent.calllog import CallLogger
from agent.checkpoint import Checkpoint, clean

MODELS = ["rules", "gpt-4o-mini", "gpt-4o"]
ENGINES = ["graph", "loop"]
NO = "n"


class LiveLogger(CallLogger):
    """The run's normal JSONL logger that also hands every record to the UI."""

    def __init__(self, on_record: Callable[[dict], None], **kw):
        super().__init__(**kw)
        self.on_record = on_record

    def log(self, record: dict) -> None:
        super().log(record)
        try:
            self.on_record({"run_id": self.run_id, **record})
        except Exception:  # the UI must never break the run or its log
            pass


def describe(record: dict) -> str | None:
    """One timeline line per trajectory record (None for records not worth a line)."""
    ev, step = record.get("event"), record.get("step")
    at = f"step {step:>2}  " if step else "         "
    if ev == "start":
        return f"start    model={record.get('model')}"
    if ev == "model":
        calls = record.get("tool_calls") or []
        if not calls:
            return f"{at}model: no tool call ({clean(record.get('text'), 60)})"
        c = calls[0]
        return f"{at}model chose {c.get('name')} {clean(c.get('arguments'), 60)}"
    if ev == "checkpoint":
        verdict = "APPROVED" if record.get("approved") else "DENIED"
        what = (record.get("proposal") or {})
        label = what.get("title") or what.get("package") or what.get("tool") or "write"
        return f"{at}you {verdict}: {clean(label, 60)}"
    if ev == "recovery":
        return f"{at}recovery: {clean(record.get('action') or record.get('kind') or record, 70)}"
    if ev == "stop":
        return f"STOP     {record.get('reason')} after {record.get('steps')} steps, ${record.get('cost_usd', 0):.4f}"
    if "tool" in record:
        status = "ok" if record.get("ok") else f"ERROR {record.get('error_type')}"
        return f"{at}{record['tool']} -> {status} ({record.get('duration_ms', 0):.0f} ms)"
    return None


class PatchPilotApp(App):
    TITLE = "PatchPilot"
    SUB_TITLE = "fix vulnerable Python pins, with you approving every write"
    CSS = """
    #form { height: auto; padding: 0 1; }
    #form Input { width: 1fr; }
    #form Select { width: 20; }
    #form Label { padding: 1 1 0 1; }
    #counter { height: 1; padding: 0 1; background: $boost; }
    #main { height: 1fr; }
    #timeline { width: 1fr; border: round $secondary; }
    #approval { width: 1fr; border: round $accent; }
    #proposal { height: 1fr; }
    #buttons { height: auto; align-horizontal: center; }
    #buttons Button { margin: 0 2; }
    """
    BINDINGS = [
        Binding("y", "answer('y')", "Approve", show=False),
        Binding("n", "answer('n')", "Deny", show=False),
        Binding("ctrl+q", "quit", "Quit"),
    ]

    def __init__(self, target: str = "", model: str = "", engine: str = "", issues: str = "",
                 prepare: Callable = cli.prepare, execute: Callable = cli.execute,
                 summary: Callable = cli.summary):
        super().__init__()
        import os

        self.initial = {
            "target": target,
            "model": model or os.environ.get("PATCHPILOT_MODEL") or "rules",
            "engine": engine or os.environ.get("PATCHPILOT_ENGINE") or cli.DEFAULT_ENGINE,
            "github": (issues or os.environ.get("PATCHPILOT_ISSUES") or "local") == "github",
        }
        self._prepare, self._execute, self._summary = prepare, execute, summary
        self.answers: queue.Queue[str] = queue.Queue()
        self.waiting = False
        self.running = False
        self.steps = 0
        self.cost = 0.0
        self.stop_reason = "-"

    # ---------- layout ----------
    def compose(self) -> ComposeResult:
        models = MODELS if self.initial["model"] in MODELS else MODELS + [self.initial["model"]]
        yield Header()
        with Horizontal(id="form"):
            yield Input(value=self.initial["target"], id="target",
                        placeholder="https://github.com/OWNER/REPO, a local folder, or empty for the template")
            yield Select([(m, m) for m in models], value=self.initial["model"], allow_blank=False, id="model")
            yield Select([(e, e) for e in ENGINES], value=self.initial["engine"], allow_blank=False, id="engine")
            yield Label("GitHub issues")
            yield Switch(value=self.initial["github"], id="github")
            yield Button("Run", id="run", variant="primary")
        yield Static(id="counter")
        with Horizontal(id="main"):
            yield RichLog(id="timeline", wrap=True, markup=False, highlight=False)
            with Vertical(id="approval"):
                yield RichLog(id="proposal", wrap=True, markup=False, highlight=False)
                with Horizontal(id="buttons"):
                    yield Button("Approve (y)", id="approve", variant="success", disabled=True)
                    yield Button("Deny (n)", id="deny", variant="error", disabled=True)
        yield Footer()

    def on_mount(self) -> None:
        self.query_one("#timeline", RichLog).border_title = "Steps"
        self.query_one("#approval").border_title = "Approval"
        self._update_counter()
        self.say("Enter a repo (or leave it empty for the template) and press Run.")
        self.say("Every edit, issue and PR waits here for your Approve.")

    # ---------- small UI helpers (UI thread) ----------
    def say(self, line: str) -> None:
        self.query_one("#timeline", RichLog).write(Text(line))

    def _update_counter(self) -> None:
        self.query_one("#counter", Static).update(
            Text(f"steps {self.steps}   cost ${self.cost:.4f}   stop reason: {self.stop_reason}"))

    def _on_record(self, record: dict) -> None:
        if record.get("event") == "model":
            self.cost = float(record.get("total_cost_usd") or self.cost)
        elif "tool" in record and record.get("step"):
            self.steps = max(self.steps, int(record["step"]))
        elif record.get("event") == "stop":
            self.stop_reason = record.get("reason") or "-"
            self.steps = record.get("steps", self.steps)
        line = describe(record)
        if line:
            self.say(line)
        self._update_counter()

    def _show_proposal(self, text: str) -> None:
        self.query_one("#proposal", RichLog).write(Text(text))

    def _open_question(self, question: str) -> None:
        self.waiting = True
        self._show_proposal(f"\n>>> {question.strip()}")
        for bid in ("#approve", "#deny"):
            self.query_one(bid, Button).disabled = False
        self.query_one("#approve", Button).focus()
        self.bell()

    def _close_question(self) -> None:
        self.waiting = False
        for bid in ("#approve", "#deny"):
            self.query_one(bid, Button).disabled = True

    # ---------- events ----------
    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "run":
            self.start_run()
        elif event.button.id == "approve":
            self.action_answer("y")
        elif event.button.id == "deny":
            self.action_answer("n")

    def on_input_submitted(self, event: Input.Submitted) -> None:
        self.start_run()

    def action_answer(self, answer: str) -> None:
        if not self.waiting:
            return
        self._close_question()
        self.answers.put(answer)

    def on_unmount(self) -> None:
        if self.waiting:  # leaving while asked is a no
            self.answers.put(NO)

    # ---------- the run ----------
    def start_run(self) -> None:
        if self.running:
            return
        self.running = True
        self.query_one("#run", Button).disabled = True
        self.steps, self.cost, self.stop_reason = 0, 0.0, "-"
        self._update_counter()
        self.query_one("#timeline", RichLog).clear()
        self.query_one("#proposal", RichLog).clear()
        params = {
            "target": self.query_one("#target", Input).value.strip() or None,
            "model_name": self.query_one("#model", Select).value,
            "engine": self.query_one("#engine", Select).value,
            "issues": "github" if self.query_one("#github", Switch).value else "local",
        }
        self.run_agent(params)

    def _ui(self, fn: Callable, *args) -> None:
        """Run fn on the UI thread; after the app has closed, drop it (the run still finishes and logs)."""
        try:
            self.call_from_thread(fn, *args)
        except Exception:
            pass

    @work(thread=True, exclusive=True)
    def run_agent(self, params: dict) -> None:
        ui = self._ui
        say = lambda line: ui(self.say, line)  # noqa: E731

        def ask(question: str) -> str:  # called by the Checkpoint, in this worker thread
            ui(self._open_question, question)
            return self.answers.get()

        try:
            logger = LiveLogger(on_record=lambda r: ui(self._on_record, r))
            p = self._prepare(logger=logger, say=say, **params)
            p.ctx.approve = Checkpoint(p.ctx, p.logger, ask=ask, show=lambda t: ui(self._show_proposal, t))
            say(cli.banner(p))
            result = self._execute(p)
            say("")
            for line in self._summary(result, p):
                say(line)
        except cli.SetupError as e:
            say(f"Cannot start: {e}")
        except Exception as e:  # show it instead of killing the UI
            say(f"Run failed: {type(e).__name__}: {clean(str(e), 200)}")
        finally:
            ui(self._finish_run)

    def _finish_run(self) -> None:
        self.running = False
        self._close_question()
        self.query_one("#run", Button).disabled = False


def main() -> None:
    PatchPilotApp().run()


if __name__ == "__main__":
    main()
