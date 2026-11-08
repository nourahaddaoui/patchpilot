# PatchPilot — The Operator (Project 3)

A vulnerability remediation agent that scans a Python repo with Trivy, bumps a vulnerable pin in `requirements.txt`, runs the tests, and commits the fix to a sandbox branch only after a human approves the diff.

## Setup

```bash
make setup
```

Creates `.venv` (the agent) and `sandbox/.venv` (the interpreter the target repo's tests run in), installs the Python dependencies, checks that Trivy is installed and downloads its vulnerability DB into `.cache/trivy` so scans are reproducible.

## Run

```bash
cp .env.example .env   # then put your OPENAI_API_KEY in .env (git-ignored, never commit it)
make run               # resets sandbox/work from the template, then runs the agent
```

The agent only ever edits `sandbox/work` (a throwaway git clone of `sandbox/template`), never the template or this repo. `make reset` rebuilds it; running it twice gives the same result. `python -m agent --no-reset` runs on the existing sandbox.

Each run prints its stop reason, step count and cost, and writes its trajectory to `logs/<run_id>.jsonl`: a `start` record, one `model` record per turn (tokens, cost), one record per tool call (input, output, time) and a final `stop` record with the named stop reason. When the run ends `done` after an approved edit, the fix is committed on branch `patchpilot/fix` inside `sandbox/work`.

The model is OpenAI `gpt-4o-mini` by default (`PATCHPILOT_MODEL` or `--model` to change it). Prices per model live in `agent/model.py`; a model without a known price is refused, because the cost cap would never fire.

On Windows use `.venv\Scripts\python -m agent` and `.venv\Scripts\python -m pytest` in place of the `make` targets.

## Test

```bash
make test
```

Call one tool by hand (logged to `logs/<run_id>.jsonl`):

```bash
.venv/bin/python -m agent.tools read_file '{"path": "requirements.txt"}'
.venv/bin/python -m agent.tools run_scan '{}'
.venv/bin/python -m agent.tools run_tests '{}'
```

`edit_file` is not callable this way: it needs a human approver, which `python -m agent` provides.

## Layout

| Path | What it is | Day |
|---|---|---|
| `agent/stop_rule.py` | Stop rule spec and `check()` | 1 |
| `sandbox/template/` | Target repo template (5 files, 3 tests, pinned vulnerable dependency) | 1 |
| `scripts/setup_trivy.sh` | Trivy check + DB cache download | 1 |
| `docs/proposal.md` | One-paragraph proposal sent to the instructor | 1 |
| `agent/tools/` | `run_scan`, `read_file`, `run_tests` (strict schemas, plain-text errors), `edit_file` | 2–3 |
| `agent/tools/registry.py` | `call_tool()`: validate, run, turn failures into plain text, log | 2 |
| `agent/calllog.py` | JSONL call log: input, output, ok, duration per call | 2 |
| `agent/tools/edit_file.py` | The only write tool: one pin in `requirements.txt`, idempotent, no downgrades, sandbox only, approval before write | 3 |
| `sandbox/reset.sh`, `agent/sandbox.py` | Rebuild `sandbox/work` from the template (or a scenario variant), baseline commit, branch `patchpilot/fix` | 3 |
| `agent/loop.py` | ReAct loop: one tool call per step, stop rule checked before every model call and every tool call | 4 |
| `agent/model.py` | `Model` interface, OpenAI implementation (cost per call from token usage), `ScriptedModel` stub for tests | 4 |
| `agent/checkpoint.py` | Human approval of the diff | 5 |
| `agent/recovery.py` | Retries, rollback, partial failure | 6 |
| `eval/` | Scenarios, fault injection, grader, results | 8–14, 16–17 |
| `threat_model.md` | Untrusted inputs and defenses | 15 |
| `errors.md` | Error taxonomy with a test per class | 13 |
| `logs/` | JSONL trajectory logs (one per run) | 2+ |

## Stop rule

The run ends on the **first** of these, in priority order:

1. Approval denied
2. No fixed version exists
3. Done — scan clean **and** tests green
4. 3 failed fix attempts
5. Cost cap — $0.50
6. Step limit — 12 steps

Once `check()` returns a reason, the loop makes no further tool calls.

The loop checks the rule before every model call and again before every tool call (so a model call that crosses the cost cap never gets its tool run). A step is one model turn with at most one tool call; a turn where the model calls no tool still counts as a step. Every turn therefore adds a step, so a run makes at most 12 model calls and 12 tool calls no matter what the model does (`tests/test_loop.py`).

## Team

| Member | Owns | Days |
|---|---|---|
| A | Tools, sandbox, recovery | 2, 3, 6 |
| B | Loop, stop rule, checkpoint, stop proof | 1, 4, 5, 17 |
| C | Scenarios, fault injection, grader, results | 8–14 |
| D | Threat model, injection cases, cost model, report | 15, 16, 18, 19 |

## Contributions
