# Error taxonomy

What PatchPilot does for each class of error, and the test that proves it. Every test in `tests/test_errors.py` runs the real loop on a fresh sandbox (real tools, checkpoint, recovery, stop rule); only trivy and pip/pytest are stand-ins that fail on purpose.

Two rules hold for every class:
- **The model never sees a stack trace.** Every failure reaches it as one plain-text line saying what went wrong and what to do (`agent/tools/registry.py`), and the call is logged with its `error_type`.
- **Every failed call still costs a step.** A failing tool or a confused model cannot keep the run alive: the 12-step cap holds.

| Class | Example | What the agent does | Ends | Test (`tests/test_errors.py`) |
|---|---|---|---|---|
| **Tool down** | trivy not installed, sandbox Python missing | Plain error `tool_down`. Not retried (retrying cannot install a program). The rules model files one handoff issue; nothing is edited. | `handed_off` | `test_tool_down_is_not_retried_and_the_run_is_handed_off` |
| **Bad arguments** | `read_file {"pathh": ...}`; a version string carrying a shell command | The registry rejects the call against the tool's strict schema before the tool runs, and lists every problem (`'path' is required; unexpected argument 'pathh'`). A write tool with bad arguments never reaches the human checkpoint. A model that corrects the call finishes normally. | as the run goes on | `test_bad_arguments_are_rejected_before_the_tool_runs`, `test_bad_arguments_to_the_write_tool_never_reach_the_human` |
| **Empty result** | trivy exits 0 with no output | Treated as `bad_output`, never as a clean scan. Retried once automatically (logged as a `recovery` retry); the retry's result is used. | `done` | `test_empty_scan_output_is_retried_once_not_taken_as_clean` |
| **Wrong tool** | the model calls `delete_file` | Plain error `unknown_tool` with the list of real tools. Nothing runs. A model that then calls the right tool finishes normally. | as the run goes on | `test_a_tool_that_does_not_exist_gets_the_list_of_real_tools` |
| **Loop** | the model repeats the same call forever; repeats an edit; stops calling tools | The step cap ends it at exactly 12 steps. A repeated edit is a no-op (`edit_file` is idempotent) and does not ask the human again. A turn with no tool call is nudged once, then the run stops. | `step_limit`, `done`, `gave_up` | `test_a_model_stuck_in_a_loop_stops_at_twelve_steps`, `test_repeating_the_same_edit_changes_nothing_and_asks_nobody`, `test_a_model_that_stops_calling_tools_is_nudged_once_then_stopped` |
| **Timeout** | the scan or the test run times out | Retried once automatically. If the retry works, the run goes on. If tests keep timing out after an edit, the edit is unverified, so `requirements.txt` is rolled back to the last tested version and a human is told. | `done` or `handed_off` | `test_scan_timeout_once_is_retried_and_the_fix_goes_through`, `test_tests_timing_out_after_an_edit_roll_the_edit_back` |
| **Tests red after an edit** | the fixed version breaks the code | The edit is rolled back at once (`agent/recovery.py`), counted as a failed fix, and the rules model files one issue instead of forcing the bump. 3 failed fixes end any run. | `handed_off` (`fix_attempts` after 3) | `test_a_bump_that_breaks_the_tests_is_rolled_back_and_handed_off` |
| **Version does not exist** | the pinned version is yanked or unknown to pip | `install_failed`: tests are not run, the edit is rolled back and counted as a failed fix. | `handed_off` | `test_a_version_pip_cannot_install_counts_as_a_failed_fix` |
| **User changes mind** | approves the first edit, denies the second; or gives no answer | Only an explicit `y` approves. A denial writes nothing and ends the run; the edit approved and tested before it stays. No input counts as no. | `approval_denied` | `test_user_approves_one_edit_then_denies_the_next`, `test_no_answer_at_all_is_a_no` |

## Recovery after failure

Of the classes above, these recover on their own and still end `done`: **empty result** (retry), **timeout once** (retry), **bad arguments** and **wrong tool** (when the model corrects its next call), and a **repeated edit** (no-op). These stop safely instead, by design, because going on would mean an unverified or unwanted change: **tool down**, **timeout on every attempt**, **tests red**, **version does not exist** (all `handed_off`, with `requirements.txt` back to its last tested version), a **loop** (`step_limit` / `gave_up`), and a **denial** (`approval_denied`).

In every class, the files at the end are either the last version the tests passed on or the original: no test in this file ends with an untested edit in `requirements.txt`.
