"""Parity and stop proof: the loop and the LangGraph engine behave the same.

Each scenario runs once per engine, on its own fresh sandbox, and must give the
expected stop reason AND tool-call sequence on both. The two engines must also
write the same JSONL trajectory (event, tool, ok, stop reason), so the stop rule,
recovery and logging are provably the same code path in both.
"""

import sys

import pytest

from agent import sandbox
from agent.calllog import CallLogger
from agent.checkpoint import Checkpoint, ScriptedHuman
from agent.graph import run_graph
from agent.loop import run
from agent.model import ModelReply, ScriptedModel
from agent.rules_model import RulesModel
from agent.stop_rule import StopReason
from agent.tools import merge_pr
from agent.tools.context import ToolContext
from tests.conftest import TEMPLATE
from tests.test_github_flow import FLOW, github_routes
from tests.test_loop import FIX, Approver, tool_records, world  # noqa: F401  (fixture)
from tests.test_open_pr import FakeGitHub

ENGINES = {"loop": run, "graph": run_graph}
LOCAL_FIX = [t for t, _ in FIX]
READ = ("read_file", {"path": "requirements.txt"})


def local_ctx(work, answer=True):
    return ToolContext(repo_root=work, test_python=sys.executable, install_deps=False, approve=Approver(answer))


def github_ctx(work):
    ctx = ToolContext(repo_root=work, test_python=sys.executable, install_deps=False,
                      issue_mode="github", github_repo="o/r", http=FakeGitHub(github_routes()))
    ctx.approve = Checkpoint(ctx, None, ask=ScriptedHuman(["y"] * 4), show=lambda s: None)
    return ctx


# name: (world setup, model factory, ctx factory, expected stop, expected tools)
SCENARIOS = {
    "template-fix-local": (None, RulesModel, local_ctx, StopReason.DONE, LOCAL_FIX),
    "github-flow": (None, RulesModel, github_ctx, StopReason.DONE, FLOW),
    "no-fixed-version": ({"fixed_version": None}, RulesModel, local_ctx, StopReason.NO_FIXED_VERSION,
                         ["run_scan", "create_issue"]),
    "tests-break-after-bump": ({"tests_green": False}, RulesModel, local_ctx, StopReason.HANDED_OFF,
                               ["run_scan", "read_file", "edit_file", "run_tests", "create_issue"]),
    "approval-denied": (None, RulesModel, lambda w: local_ctx(w, False), StopReason.APPROVAL_DENIED,
                        ["run_scan", "read_file", "edit_file"]),
    "cost-cap": (None, lambda: ScriptedModel(FIX, cost_per_call=0.2), local_ctx, StopReason.COST_CAP,
                 ["run_scan", "read_file"]),
    "never-finishes": (None, lambda: ScriptedModel([READ]), local_ctx, StopReason.STEP_LIMIT, ["read_file"] * 12),
    "never-calls-a-tool": (None, lambda: ScriptedModel([ModelReply(text="hmm")]), local_ctx,
                           StopReason.STEP_LIMIT, []),
}


def trajectory(logger):
    """What both engines must log identically (no timings, no ids)."""
    return [(r.get("event"), r.get("tool"), r.get("ok"), r.get("reason"), r.get("step"))
            for r in logger.read() if r.get("event") != "checkpoint"]


@pytest.fixture(autouse=True)
def no_wait(monkeypatch):
    monkeypatch.setenv("GITHUB_TOKEN", "t")
    monkeypatch.setattr(merge_pr, "_sleep", lambda s: None)


@pytest.mark.parametrize("name", list(SCENARIOS))
def test_both_engines_stop_the_same_way(name, world, tmp_path):
    setup, make_model, make_ctx, expected_reason, expected_tools = SCENARIOS[name]
    for key, value in (setup or {}).items():
        setattr(world, key, value)

    seen = {}
    for engine, drive in ENGINES.items():
        work = sandbox.reset(TEMPLATE, tmp_path / engine)
        logger = CallLogger(run_id=engine, log_dir=tmp_path / "logs")
        result = drive(make_model(), make_ctx(work), logger=logger)
        tools = [r["tool"] for r in tool_records(logger)]
        assert result.stop_reason is expected_reason, f"{engine}: {result.stop_reason}"
        assert tools == expected_tools, f"{engine}: {tools}"
        assert result.state.steps <= 12
        seen[engine] = (result.stop_reason, tools, result.state.steps, bool(result.commit), trajectory(logger))

    assert seen["loop"] == seen["graph"]
