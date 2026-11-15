"""The free rule-based model, driven through the real loop."""

import pytest

from agent.loop import run
from agent.rules_model import RulesModel, fix_version, plan_fixes, transcript
from agent.stop_rule import StopReason
from agent.tools import create_issue
from tests.test_loop import logger, make_ctx, tool_records, work, world  # noqa: F401  (fixtures)


def tools_called(logger):
    return [r["tool"] for r in tool_records(logger)]


def test_fixes_the_repo_for_free(world, make_ctx, logger, work):
    result = run(RulesModel(), make_ctx(True), logger=logger)
    assert result.stop_reason is StopReason.DONE and result.commit
    assert tools_called(logger) == ["run_scan", "read_file", "edit_file", "run_tests", "run_scan"]
    assert result.state.cost_usd == 0
    assert (work / "requirements.txt").read_text().strip() == "requests==2.31.0"


def test_no_fixed_version_files_an_issue(world, make_ctx, logger, work):
    world.fixed_version = None
    result = run(RulesModel(), make_ctx(True), logger=logger)
    assert result.stop_reason is StopReason.NO_FIXED_VERSION
    assert tools_called(logger) == ["run_scan", "create_issue"]
    assert len(list((work / create_issue.STORE).glob("*.json"))) == 1


def test_broken_tests_hand_off_after_rollback(world, make_ctx, logger, work):
    world.tests_green = False
    result = run(RulesModel(), make_ctx(True), logger=logger)
    assert result.stop_reason is StopReason.HANDED_OFF
    assert tools_called(logger)[-1] == "create_issue"
    assert (work / "requirements.txt").read_text().strip() == "requests==2.25.1"  # rolled back


def test_denied_edit_stops(world, make_ctx, logger, work):
    result = run(RulesModel(), make_ctx(False), logger=logger)
    assert result.stop_reason is StopReason.APPROVAL_DENIED
    assert (work / "requirements.txt").read_text().strip() == "requests==2.25.1"


def test_transcript_pairs_calls_with_results_and_reads_json_before_notes():
    msgs = [
        {"role": "system", "content": "s"},
        {"role": "assistant", "content": None, "tool_calls": [
            {"id": "a", "type": "function", "function": {"name": "run_tests", "arguments": "{}"}}]},
        {"role": "tool", "tool_call_id": "a", "content": '{"green": false}\n[recovery] rolled back'},
        {"role": "assistant", "content": None, "tool_calls": [
            {"id": "b", "type": "function", "function": {"name": "read_file", "arguments": '{"path": "x"}'}}]},
        {"role": "tool", "tool_call_id": "b", "content": "ERROR (read_file): not allowed"},
    ]
    steps = transcript(msgs)
    assert [(s.tool, s.ok) for s in steps] == [("run_tests", True), ("read_file", False)]
    assert steps[0].data == {"green": False} and steps[1].args == {"path": "x"}


@pytest.mark.parametrize("installed, fixed, want", [
    ("2.25.1", "2.32.4", "2.32.4"),
    ("1.26.5", "1.26.18, 2.0.7", "1.26.18"),
    ("2.0.0", "1.26.18, 2.0.7", "2.0.7"),
    ("2.25.1", "", None),
    ("2.25.1", "2.0.0", None),
])
def test_fix_version(installed, fixed, want):
    got = fix_version(installed, fixed)
    assert (str(got) if got else None) == want


def test_plan_takes_highest_fix_and_skips_files_it_cannot_edit():
    scan = {"vulnerabilities": [
        {"package": "requests", "installed": "2.25.1", "fixed": "2.31.0", "file": "requirements.txt"},
        {"package": "requests", "installed": "2.25.1", "fixed": "2.32.4", "file": "requirements.txt"},
        {"package": "lodash", "installed": "4.17.0", "fixed": "4.17.21", "file": "package-lock.json"},
        {"package": "idna", "installed": "2.10", "fixed": None, "file": "requirements.txt"},
    ]}
    assert plan_fixes(scan) == {"requests": "2.32.4"}
