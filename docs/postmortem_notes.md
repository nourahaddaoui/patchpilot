# Postmortem notes

## 2026-11-19: the test suite filed real issues on the demo repo

**What happened.** During the Phase 1 (GitHub mode) work, `python run.py test` was run several times while `.env` held `PATCHPILOT_ISSUES=github`, `PATCHPILOT_GITHUB_REPO=azizbenzarti/patchpilot-demo` and a real `GITHUB_TOKEN`. Two tests filed real issues on the demo repo: #1 "No fixed version available for requests" and #2 "Bumping a vulnerable pin breaks the tests". Later runs found these issues by title and reused them instead of filing new ones, but each run still read the repo's open issues over the live API. The problem showed up as an order-dependent failure: `test_rules_model.py::test_no_fixed_version_files_an_issue` passed on its own and failed in the full suite.

**Root cause.** `tests/test_model.py` builds `OpenAIModel(...)`, which calls `load_dotenv()`. That copied the real `.env` into `os.environ` for the rest of the pytest process. Every later test that built a `ToolContext` without setting `issue_mode` read `PATCHPILOT_ISSUES=github` from the environment. When `create_issue` ran with no fake HTTP transport injected, it used the real one (urllib) with the real token.

**Impact.**
- Two stray issues on `azizbenzarti/patchpilot-demo`, closed by hand.
- No secret was exposed outside this machine: the token was only sent to api.github.com in the Authorization header, and the tool never logs or returns it.
- Nothing else in the repo was changed.

**Fix.** Commit `5c25748` added an autouse fixture, `offline`, in `tests/conftest.py`, so for every test:
- `load_dotenv` does nothing;
- `PATCHPILOT_ISSUES`, `PATCHPILOT_GITHUB_REPO`, `GITHUB_TOKEN`, `PATCHPILOT_ADMIN_TOKEN`, `OPENAI_API_KEY` and `PATCHPILOT_MODEL` are removed;
- the real GitHub transport raises, so a test that forgets to inject a fake fails instead of calling GitHub.

The regression test `tests/test_no_live_github.py` runs the test files involved in the incident, in the order that triggered it, in a child pytest. That child starts with a real-looking token and github mode already in its environment, and every outbound connection is blocked and recorded. The test passes only if the child passes and records zero connections. With the fixture disabled, it fails.

**Lesson.** Tests must never inherit real credentials. Isolation belongs in one autouse place, not in each test's good intentions. A default that reads the environment (`ToolContext.issue_mode`) must not be able to switch a test run to a live service. A suite that passes alone but fails in the full run should be treated as a possible leak until the cause is known.
