# Postmortem

The real problems we hit building PatchPilot, what caused them, how we fixed them, and what we learned. Blameless: the point is the system, not who typed the command. Every fix links to its commit or file.

## 1. The test suite filed real issues on GitHub (2026-11-19)

**What happened.** Running the full test suite with GitHub mode configured in `.env` filed two real issues on the demo repo. A test that passed alone failed in the full run.
**Root cause.** One test built an `OpenAIModel`, which loaded the developer's `.env` into the environment for the rest of the test process. Later tests read `PATCHPILOT_ISSUES=github` and a real token from it and called the live API.
**Fix.** An autouse fixture removes every live setting and makes the real GitHub transport fail inside tests (`5c25748`), plus a regression test that reruns the incident in a child process with network access blocked and recorded (`1ac5ad1`). Full write-up: `docs/postmortem_notes.md`.
**Lesson.** Tests must never inherit real credentials, and that rule belongs in one place every test passes through, not in each test.

## 2. The first fix for it was incomplete (2026-11-21)

**What happened.** After fix 1, a new model class still loaded the real `.env` during a test.
**Root cause.** The fixture patched `agent.model.load_dotenv`, but modules that had done `from agent.model import load_dotenv` kept the original function.
**Fix.** `load_dotenv()` reads its path at call time and the fixture sets that path to `None`, which covers every importer; a regression test calls each importer and asserts on a boolean only, so a failing test can never print a secret (`181b00f`).
**Lesson.** Patching a function by name does not patch the copies other modules imported. Patch the data the function reads, and test every path, not the one you thought of.

## 3. "Clean" needed a bigger bump than the plan assumed (2026-11-08)

**What happened.** The plan and the template's README said `requests 2.25.1` has one CVE, fixed in 2.31.0. Trivy reports three (CVE-2023-32681, CVE-2024-35195, CVE-2024-47081), so a clean scan needs 2.32.4. Two releases on the way (2.32.0 and 2.32.1) were yanked from PyPI.
**Root cause.** The target was chosen from one advisory instead of from a scan.
**Fix.** The rules model pins the smallest version that fixes **every** finding for a package (`agent/rules_model.py: plan_fixes`), and a pin pip cannot install counts as a failed fix and is rolled back (`agent/recovery.py`).
**Lesson.** Check assumptions against the tool's real output before designing around them. "Done" must mean "scan clean", not "the CVE we had in mind is gone".

## 4. Setup did not work on every teammate's machine (2026-11-08)

**What happened.** On Windows, `reset.sh` ran under the WSL `bash` instead of Git Bash, the sandbox interpreter was looked up in `bin/` instead of `Scripts\`, and the symlink test failed for users who cannot create symlinks. On another machine the ~1.6 GB Trivy database did not fit on the repo's disk.
**Fix.** Windows paths and Git Bash (`b15cd04`), a `PATCHPILOT_TRIVY_CACHE` setting for the database location (`b67c4c0`), and later a single cross-platform `python scripts/setup.py` with no `make` or `bash` (`d0d35e8`).
**Lesson.** The README's one setup command must be tested on every OS the team uses, from a fresh clone, early.

## 5. Real repositories broke assumptions the template never tested (2026-11-22)

**What happened.** Pointed at real GitHub repos, runs crashed on non-UTF-8 subprocess output, reported "no tests" for repos that use `unittest` in a `tests.py`, and could not import the project under test.
**Fix.** UTF-8 decoding with replacement, a `unittest discover` fallback when pytest finds nothing, and an optional `pip install -e .` for the project (`d7601ff`).
**Lesson.** A five-file template proves the design, not the robustness. Run the agent on real repos as soon as the loop works.

## 6. The checkpoint over-claimed what an edit fixed (2026-11-22)

**What happened.** The human checkpoint showed findings in other files (for example a lock file) as "fixed" by an edit to `requirements.txt`, because the "before" scan covered the whole repo and the "after" preview only the proposed file.
**Root cause.** The two scans compared different scopes.
**Fix.** The "before" scan now covers only the edited file; findings elsewhere are listed separately, never as fixed (`1ff8349`, `tests/test_checkpoint.py::test_findings_in_other_files_are_not_shown_as_fixed`).
**Lesson.** The checkpoint is the last safety layer. If it shows the human something misleading, the human approves the wrong thing. It needs tests as strict as the write tools.

## 7. A model that stopped calling tools burned the step budget

**What happened.** A model that answered in plain text instead of calling a tool was nudged on every turn until it hit the 12-step limit, wasting steps and, with a paid model, money.
**Fix.** A `gave_up` stop reason after two turns in a row without a tool call (`1ff8349`, proven in `logs/proof/gave_up.jsonl`).
**Lesson.** Every way a run can fail to make progress needs its own named stop, not only the global cap.

## 8. Two people built the same eval suite (2026-11-22 to 11-23)

**What happened.** Two team members built a full Week 2 eval suite in parallel on separate branches. One was merged (PR #10); the other was thrown away unpushed, about a day of duplicated work.
**Root cause.** Ownership in the plan had drifted from what people were actually doing, and nobody said "I am starting this" before starting.
**Fix.** The remaining Week 3 items (stop-rule proof, error taxonomy, threat model, credential scan, postmortem) were claimed in the team chat before work began, and built only where nothing overlapped.
**Lesson.** Claim a task out loud, or open a draft pull request, before writing code. A branch nobody can see is a collision waiting to happen.

## What went well

- **The stop rule was written first.** Every later feature (GitHub mode, the TUI, LangGraph) had to fit it, and it held: one logged run per stop reason, and no eval run ever exceeded it.
- **Safety lives in the tools, not the prompt.** When the eval used a model that obeys every injected instruction, every attack was still stopped by a tool guard or the human checkpoint.
- **Plain-code grading.** The eval needs no model to grade, so results are free, fast and exactly reproducible.

## What we would do differently

- Scan the target first and design from the real findings (problem 3).
- Run on real repos and on every OS in Week 1, not Week 3 (problems 4 and 5).
- Keep a visible task board with owners, and update it when plans change (problem 8).
