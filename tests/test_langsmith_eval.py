"""The eval on LangSmith (eval/langsmith_eval.py): dataset, evaluators, a full experiment.

Nothing goes over the network: the experiment runs on tracing.make_client() built on the
recording session from test_tracing.py, with in-memory examples and upload_results=False.
"""

import json
import shutil
import uuid
from datetime import datetime, timezone

import pytest
from langsmith import schemas, tracing_context

from agent.tracing import make_client
from eval import langsmith_eval as le
from eval.runner import Options, run_scenario
from eval.scenarios import BY_ID, SCENARIOS
from tests.test_tracing import FakeResponse, RecordingSession

needs_git = pytest.mark.skipif(shutil.which("git") is None, reason="git not installed")


class Reply(FakeResponse):
    def __init__(self, data):
        self._data, self.text = data, json.dumps(data)
        self.content = self.text.encode()

    def json(self):
        return self._data


class FakeLangSmith(RecordingSession):
    """Records every request like RecordingSession, and answers 'create experiment' the way LangSmith does."""

    session_ = None

    def request(self, method, url, *a, **k):
        super().request(method, url, *a, **k)
        if "/sessions" in url.split("?")[0] and method in ("POST", "PATCH"):
            body = json.loads(self.calls[-1][2] or "{}")
            if self.session_ is None:
                self.session_ = {"id": str(uuid.uuid4()), "tenant_id": str(uuid.uuid4()),
                                 "name": body.get("name", "x"), "reference_dataset_id": body.get("reference_dataset_id"),
                                 "start_time": datetime.now(timezone.utc).isoformat()}
            return Reply(self.session_)
        return Reply({})


@pytest.fixture
def recording():
    session = FakeLangSmith()
    client = make_client(api_key="fake-key", api_url="http://langsmith.invalid", session=session,
                         auto_batch_tracing=False)
    return client, session


def examples(ids):
    ds = uuid.uuid4()
    out = []
    for sid in ids:
        e = le.example_for(BY_ID[sid])
        out.append(schemas.Example(id=uuid.uuid4(), dataset_id=ds, inputs=e["inputs"], outputs=e["outputs"]))
    return out


# --- dataset ----------------------------------------------------------------------------------

def test_every_scenario_becomes_one_example_with_its_reference_trajectory():
    e = le.example_for(BY_ID["fix-03-two-packages"])
    assert e["inputs"]["scenario_id"] == "fix-03-two-packages"
    assert e["outputs"]["expected_tools"] == BY_ID["fix-03-two-packages"].expected_tools
    assert e["outputs"]["expected_stop"] == "done"
    assert e["split"] == "dev" and e["metadata"]["category"] == "fix"
    assert le.example_for(BY_ID["inj-01-scan-title-downgrade"])["metadata"]["attack_surface"] == "scan title"


def test_dataset_name_changes_when_a_scenario_changes():
    a = le.dataset_name(SCENARIOS)
    assert a.startswith("patchpilot-scenarios-") and a == le.dataset_name(SCENARIOS)
    assert le.dataset_name(SCENARIOS[:-1]) != a


class FakeDatasets:
    def __init__(self, exists):
        self.exists, self.created, self.examples = exists, [], []

    def has_dataset(self, dataset_name):
        return self.exists

    def create_dataset(self, name, description=None):
        self.created.append(name)

    def create_examples(self, dataset_name, examples):
        self.examples.extend(examples)


def test_dataset_is_created_once_with_all_examples():
    fresh = FakeDatasets(exists=False)
    name = le.ensure_dataset(fresh)
    assert fresh.created == [name] and len(fresh.examples) == len(SCENARIOS)
    existing = FakeDatasets(exists=True)
    assert le.ensure_dataset(existing) == name and existing.created == [] and existing.examples == []


# --- evaluators ---------------------------------------------------------------------------------

def scores(result):
    return {r["key"]: r["score"] for r in result["results"]}


def test_trajectory_evaluator_uses_the_reference_from_the_dataset():
    ref = {"expected_tools": ["run_scan", "read_file", "edit_file"], "expected_stop": "done", "safe_stops": []}
    s = scores(le.trajectory({"tools": ["run_scan", "run_scan", "read_file", "edit_file"], "stop_reason": "done"}, ref))
    assert s == {"step_accuracy": 0.75, "path_recall": 1.0, "stop_reason_correct": True}
    s = scores(le.trajectory({"tools": ["run_scan"], "stop_reason": "step_limit"}, ref))
    assert s["path_recall"] == pytest.approx(1 / 3, abs=1e-3) and s["stop_reason_correct"] is False
    injected = {**ref, "safe_stops": ["handed_off"]}
    assert scores(le.trajectory({"tools": [], "stop_reason": "handed_off"}, injected))["stop_reason_correct"]


def test_safety_and_task_evaluators():
    row = {"no_write_before_approval": True, "stop_rule_held": True, "only_intended_changes": True,
           "harm": False, "attack_attempted": True, "blocked_by": "tool guard", "success": True,
           "steps": 3, "cost_usd": 0.0, "recovered": None, "tool_calls": 3, "precise_calls": 2, "wasted_steps": 1}
    assert scores(le.safety(row))["injection_blocked"] is True
    assert "injection_blocked" not in scores(le.safety({**row, "harm": None}))
    assert "recovered_after_failure" not in scores(le.task(row))
    assert scores(le.task({**row, "recovered": True}))["recovered_after_failure"] is True
    assert scores(le.tool_calls(row)) == {"tool_call_precision": 0.667, "wasted_steps": 1}


def test_summary_evaluator():
    rows = [{"success": True, "stop_rule_held": True, "elapsed_s": 1.0, "cost_usd": 0.0, "recovered": True},
            {"success": False, "stop_rule_held": True, "elapsed_s": 3.0, "cost_usd": 0.0, "harm": False}]
    s = scores(le.summary(rows, [{}, {}]))
    assert s["success_rate"] == 0.5 and s["recovery_rate"] == 1.0 and s["injection_harm_runs"] == 0
    assert s["p95_time_s"] == 3.0


# --- a whole experiment, offline -------------------------------------------------------------------

@needs_git
def test_experiment_runs_scores_and_traces_every_scenario(recording):
    client, session = recording
    ids = ["fix-01-one-cve", "fail-01-scan-timeout-once", "inj-01-scan-title-downgrade"]
    local, results = le.run_experiment([BY_ID[i] for i in ids], Options("compromised", "graph"), repeats=1,
                                       client=client, data=examples(ids))
    client.flush()
    rows = list(results)
    assert sorted(r["run"].outputs["scenario"] for r in rows) == sorted(ids)
    for r in rows:
        s = {e.key: e.score for e in r["evaluation_results"]["results"]}
        assert s["task_success"] is True and s["no_write_before_approval"] is True and s["stop_rule_held"] is True
    inj = next(r for r in rows if r["run"].outputs["scenario"] == "inj-01-scan-title-downgrade")
    assert {e.key: e.score for e in inj["evaluation_results"]["results"]}["injection_blocked"] is True
    assert local["summary"]["success_rate"] == 1.0  # the same rows feed eval/results/

    sent = " ".join(body or "" for _, _, body in session.calls)
    paths = [url.split("?")[0] for _, url, _ in session.calls]
    assert any(p.endswith("/sessions") for p in paths)  # the experiment
    assert any(p.endswith("/feedback") for p in paths)  # the evaluator scores
    for span in ('"model_call"', '"run_scan"', '"checkpoint_decision"'):  # the agent's steps, inside the trace
        assert span in sent
    for key in ("step_accuracy", "tool_call_precision", "no_write_before_approval", "injection_blocked"):
        assert f'"{key}"' in sent
    assert "eval-fake-token" not in sent  # secrets are redacted before anything leaves


@needs_git
def test_offline_eval_sends_nothing_even_inside_a_tracing_context(recording, tmp_path):
    client, session = recording
    with tracing_context(enabled=True, client=client):
        row = run_scenario(BY_ID["fix-01-one-cve"], Options("rules", "graph"), tmp_path)  # trace=False
    client.flush()
    assert row["success"] and session.calls == []


def test_langsmith_flag_needs_an_api_key(monkeypatch, capsys):
    from eval.__main__ import main

    monkeypatch.delenv("LANGSMITH_API_KEY", raising=False)
    assert main(["--langsmith", "--only", "fix-01-one-cve", "--repeats", "1"]) == 2
    assert "LANGSMITH_API_KEY" in capsys.readouterr().err
