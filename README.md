# PatchPilot — The Operator (Project 3)

A vulnerability remediation agent that scans a Python repo with Trivy, bumps a vulnerable pin in `requirements.txt`, runs the tests, and commits the fix to a sandbox branch only after a human approves the diff.

## Setup

```bash
python scripts/setup.py
```

Works the same on Windows, macOS and Linux (Python 3.10+, Git and Trivy installed; no `make` or `bash` needed). It creates `.venv` (the agent) and `sandbox/.venv` (the interpreter the template repo's tests run in), installs the dependencies, and downloads Trivy's vulnerability DB once into `.cache/trivy` so scans are offline and reproducible. Running it again keeps what is there.

## Run

```bash
python run.py
```

That fixes the template repo with the free rule-based model: no API key, no cost. Every edit and every issue waits for your `y`.

| Command | What it does |
|---|---|
| `python run.py` | Template repo, free `rules` model |
| `python run.py --target https://github.com/OWNER/REPO` | A real repo: cloned into the sandbox, fixed there |
| `python run.py --target C:/code/my-repo` | A local repo (its committed files only) |
| `python run.py --model gpt-4o-mini` | GPT chooses the tools; needs `OPENAI_API_KEY` in `.env`; cost counted against the $0.50 cap |
| `python run.py --no-reset` | Run again on the existing `sandbox/work` |

**Real repos are never edited in place.** A target is cloned into `sandbox/sources/<name>`, copied into the throwaway `sandbox/work`, and fixed there; its tests run in their own interpreter (`sandbox/.venvs/<name>`). A fix that ends `done` is committed on branch `patchpilot/fix` inside `sandbox/work` (the run prints `git -C sandbox/work show <sha>`); pushing it to the real repo is up to you. Only a root `requirements.txt` with `==` pins can be edited; findings elsewhere (other lockfiles) end in an issue for a human. A repo with no tests can never reach `done`, because the fix cannot be verified.

**Running a real repo's tests runs its code on your machine** (the sandbox is a folder and a venv, not a container). Only point PatchPilot at repos you trust.

Each run prints its stop reason, step count and cost, and writes its trajectory to `logs/<run_id>.jsonl`: a `start` record, one `model` record per turn (tokens, cost), one record per tool call (input, output, time), `checkpoint` and `recovery` events, and a final `stop` record with the named stop reason.

GPT models: prices live in `agent/model.py`; a model without a known price is refused, because the cost cap would never fire. `PATCHPILOT_MODEL` sets the default model.

The Trivy DB is about 1.6 GB unpacked. To keep it on another disk, set `PATCHPILOT_TRIVY_CACHE` (in your environment or `.env`) before `python scripts/setup.py`.

## Recovery

| Failure | What happens |
|---|---|
| Scanner or test run times out, or returns garbled output | Retried once automatically (logged as a `recovery` retry). Write tools are never retried. |
| Tests fail right after an edit | `requirements.txt` goes back to the last version the tests passed on; the model is told and can try another version or file an issue. |
| Run ends early (limit, denial) with an untested edit | The edit is reverted; edits the tests already passed on stay. |

## Test

```bash
python run.py test
```

Call one tool by hand (logged to `logs/<run_id>.jsonl`):

```bash
python -m agent.tools read_file '{"path": "requirements.txt"}'
python -m agent.tools run_scan '{}'
python -m agent.tools run_tests '{}'
```

`edit_file` and `create_issue` are not callable this way: they need a human approver, which `python -m agent` provides.

## Filing issues (`create_issue`)

The agent can hand a problem to a human: a vulnerability with no fixed version, or a fix that breaks the tests. The checkpoint shows the exact title and body and asks `File this issue? [y/n]`. Filing the same title twice returns the existing issue, `@mentions` are put in code spans so nobody is pinged, and a run files at most one issue.

| Mode | Set | Where the issue goes |
|---|---|---|
| `local` (default) | nothing | `logs/issues/<time>-NNNN.json` (git-ignored, survives sandbox resets) |
| `github` | `PATCHPILOT_ISSUES=github`, `PATCHPILOT_GITHUB_REPO=owner/name`, `GITHUB_TOKEN` | A real issue on that repo. Use a test repo. The token is never logged. |

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
| `agent/tools/edit_file.py` | Write tool: one pin in `requirements.txt`, idempotent, no downgrades, sandbox only, approval before write | 3 |
| `agent/tools/create_issue.py` | Write tool: hand a problem to a human as an issue (local file or GitHub), idempotent by title, approval before filing | 5 |
| `sandbox/reset.sh`, `agent/sandbox.py` | Rebuild `sandbox/work` from the template (or a scenario variant), baseline commit, branch `patchpilot/fix` | 3 |
| `agent/loop.py` | ReAct loop: one tool call per step, stop rule checked before every model call and every tool call | 4 |
| `agent/model.py` | `Model` interface, OpenAI implementation (cost per call from token usage), `ScriptedModel` stub for tests | 4 |
| `agent/checkpoint.py` | Human checkpoint: full diff, real scan before and after (the proposed file is scanned in a temp folder), what it fixes / leaves open / introduces, then y/n. Only an explicit yes approves; no input is a no. Every decision is logged. `ScriptedHuman` stands in for the human in evals | 5 |
| `agent/recovery.py` | Retries, rollback to the last tested-good file, cleanup after partial failure | 6 |
| `agent/rules_model.py` | `RulesModel`: free, deterministic model behind the same interface (the default; the eval baseline) | 6 |
| `agent/targets.py` | Real repos as targets: clone into `sandbox/sources`, per-target test interpreter | 6 |
| `scripts/setup.py`, `run.py` | One setup command and one run command, any OS | 7 |
| `eval/` | Scenarios, fault injection, grader, results | 8–14, 16–17 |
| `threat_model.md` | Untrusted inputs and defenses | 15 |
| `errors.md` | Error taxonomy with a test per class | 13 |
| `logs/` | JSONL trajectory logs (one per run) | 2+ |

## Stop rule

The run ends on the **first** of these, in priority order:

1. Approval denied
2. No fixed version exists
3. Done — scan clean **and** tests green
4. Handed off — the agent filed an issue for a human
5. 3 failed fix attempts
6. Cost cap — $0.50
7. Step limit — 12 steps

Once `check()` returns a reason, the loop makes no further tool calls. One exception: after "no fixed version", a single `create_issue` call is still allowed so a human hears about it; the reason then stays `no_fixed_version`.

The loop checks the rule before every model call and again before every tool call (so a model call that crosses the cost cap never gets its tool run). A step is one model turn with at most one tool call; a turn where the model calls no tool still counts as a step. Every turn therefore adds a step, so a run makes at most 12 model calls and 12 tool calls no matter what the model does (`tests/test_loop.py`).

## Team

| Member | Owns | Days |
|---|---|---|
| A | Tools, sandbox, recovery | 2, 3, 6 |
| B | Loop, stop rule, checkpoint, stop proof | 1, 4, 5, 17 |
| C | Scenarios, fault injection, grader, results | 8–14 |
| D | Threat model, injection cases, cost model, report | 15, 16, 18, 19 |

## Contributions
