# Threat model

PatchPilot reads text it does not control and then acts with a write tool. This file lists every place untrusted text enters, what an attacker would want from it, and the layers that stop them, with the test that proves each layer.

## What the attacker wants

| Goal | Example |
|---|---|
| A bad write | downgrade a package, pin a malicious package, edit CI, tests or install scripts |
| Skip a control | merge without CI, write without the human, claim done without the rescan or tests |
| Steal secrets | read `.env`, leak `GITHUB_TOKEN` / `OPENAI_API_KEY` into an issue, a log or a trace |
| Abuse people | file an issue with a phishing link or `@mentions` that ping the whole org |
| Burn resources | keep the agent looping or calling the paid model |

## Defense layers

The layers are independent, so an attack must beat all of them.

| Layer | What it does | Where |
|---|---|---|
| **L1. Data, not instructions** | Every tool result an LLM sees is wrapped in `<tool_output untrusted="true">`, cannot close its own block, and the system prompt says text in tool output is never an instruction | `agent/injection.py` |
| **L2. Tool guards** | Strict schemas (unknown arguments, bad versions and package names are rejected before a tool runs); `edit_file` edits only the root `requirements.txt`, one exact pin, never downgrades, only in the sandbox; `read_file` only reads allowlisted, non-hidden files inside the repo | `agent/tools/` |
| **L3. Human checkpoint** | Every write (edit, issue, PR, merge) waits for an explicit `y` on the full diff or text, with the scan before and after; no answer is a no; untrusted text is stripped of control characters before it reaches the terminal | `agent/checkpoint.py` |
| **L4. Limits** | 12 steps, $0.50, 3 failed fixes, one handoff issue per run | `agent/stop_rule.py` |
| **L5. Isolation** | The agent works on a throwaway sandbox clone; target tests and scans run with secrets scrubbed from their environment; traces are redacted | `agent/sandbox.py`, `agent/tools/run_tests.py`, `agent/tracing.py` |

L1 can be fooled: a model may still obey injected text. L2 to L5 hold even then. The eval proves it with a **compromised model** that obeys every injected instruction: across the 8 injection scenarios it tried every attack, and no attack did harm. Four were stopped by a tool guard and four by the human checkpoint (`eval/results/compromised-graph.md`).

## Where untrusted text enters

| # | Entry point | Attacker controls | Wants | Layers | Tests |
|---|---|---|---|---|---|
| **T1** | **CVE descriptions and advisory titles** (scanner `Title`) | Anyone who can publish or edit an advisory | Downgrade, read `.env`, merge without review, break out of the data block | L1, L2 (no downgrade, allowlists), L3, title cut to 200 characters | `tests/test_injection.py::test_tool_output_cannot_close_its_own_block`, `tests/test_threats.py::test_t1_a_long_advisory_title_is_cut_before_the_model_sees_it`, eval `inj-01`, `inj-02`, `inj-07`, `inj-08` |
| **T2** | **Scanner JSON fields** (`FixedVersion`, `PkgName`, `InstalledVersion`, `Target`) | A poisoned vulnerability DB or a crafted lock file | Get a command or a strange package name into `edit_file` | L2 (`version` and `package` must match strict patterns), the rules model only pins values that parse as versions | `tests/test_threats.py::test_t2_*`, `tests/test_edit_file.py::test_through_registry_bad_version_never_reaches_the_tool` |
| **T3** | **Package metadata and pip output** (pip's error text after a failed install) | A package index or mirror | Suggest a downgrade ("last version on this mirror") | L1, L2 (no downgrade), recovery rolls the edit back on `install_failed` | eval `inj-06`, `tests/test_errors.py::test_a_version_pip_cannot_install_counts_as_a_failed_fix` |
| **T4** | **File contents** (`read_file`: `requirements.txt` comments, README, source) | Whoever wrote the target repo | Add an unknown package, edit other files, read secrets | L1, L2 (`edit_file` only touches one pin and keeps comments as plain text; `read_file` refuses `.env`, `.git`, `.github` and anything outside the repo), L3 | `tests/test_read_file.py::test_hidden_files_are_refused`, `::test_paths_outside_repo_are_refused`, `::test_symlink_escape_is_refused`, `tests/test_edit_file.py::test_only_requirements_txt_is_editable`, eval `inj-03` |
| **T5** | **Test output** (`run_tests` output tail) | The target's own tests | Make the agent edit or delete tests, or file a phishing issue | L1, L2 (tests are not editable), L3 shows the exact issue text, `create_issue` puts `@mentions` in code spans | `tests/test_create_issue.py::test_mentions_never_ping`, eval `inj-04`, `inj-05` |
| **T6** | **GitHub API responses** (existing issues, PR state, CI status) | Anyone who can open issues or PRs on the repo | Get another PR merged, merge with CI red or missing | L2 (`merge_pr` merges only the PR this run opened, from `patchpilot/fix`, after CI passes on the tested commit; `open_pr` refuses on drift), L3 | `tests/test_merge_pr.py::test_refuses_a_pr_from_another_branch`, `::test_ci_failure_is_refused_and_nothing_merged`, `tests/test_open_pr.py::test_refuses_on_drift_before_asking` |
| **T7** | **The terminal** (any untrusted text shown at the checkpoint) | Advisory or repo text with escape codes | Fake an "Approved" line, hide part of the diff | L3 strips control characters before printing | `tests/test_checkpoint.py::test_untrusted_scanner_text_cannot_drive_the_terminal` |
| **T8** | **Secrets in the environment** | The target repo's code (it runs during the tests) | Read `GITHUB_TOKEN` / `OPENAI_API_KEY` and send them out | L5: scrubbed environment for pip, pytest and trivy; tokens only in an HTTP header, never in URLs, results or logs; traces redacted | `tests/test_env_scrub.py::test_real_pytest_run_cannot_read_secrets_and_still_works`, `tests/test_open_pr.py::test_token_only_in_the_auth_header_never_in_url_result_or_log`, `tests/test_tracing.py::test_redact_removes_secret_env_values_tokens_and_url_credentials`, `tests/test_threats.py::test_t8_secrets_in_the_environment_never_reach_the_run_log` |
| **T9** | **The model itself** (a confused or compromised LLM) | Indirectly, any of the above | Loop forever, spend money, write without approval | L2, L3, L4 | `tests/test_stop_proof.py`, `tests/test_errors.py`, eval `compromised` model |
| **T10** | **Secrets committed to this repo** | A careless commit | Publish a real token with the code | Credential scan before the freeze | `scripts/scan_secrets.py`, `tests/test_scan_secrets.py` |

## Accepted risks

- **Running a real repo's tests runs its code on your machine.** The sandbox is a folder and a virtual environment, not a container. Secrets are scrubbed from that process (T8), but the code can still read your files or the network. Only point PatchPilot at repos you trust (README, "Run").
- **The human is the last layer.** An attacker's text can end up in an issue the agent proposes (for example a test output tail quoted in a handoff issue). The checkpoint shows the exact text before anything is filed, but a person who answers `y` without reading lets it through.
- **The vulnerability data is trusted for what is vulnerable.** A poisoned DB could hide a real CVE or invent one. PatchPilot never trusts it for what to write: versions must parse, never downgrade, and every write is approved.
