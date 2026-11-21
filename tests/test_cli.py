"""CLI wiring: --engine picks the driver, --model picks the model class. No network."""

import shutil

import pytest

import agent.__main__ as cli
from agent import sandbox
from agent.graph import run_graph
from agent.lc_model import LangChainModel
from agent.loop import RunResult, run
from agent.model import OpenAIModel
from agent.rules_model import RulesModel
from agent.stop_rule import RunState, StopReason
from tests.conftest import TEMPLATE


@pytest.fixture
def box(tmp_path):
    if shutil.which("git") is None:
        pytest.skip("git not installed")
    return sandbox.reset(TEMPLATE, tmp_path / "work")


def test_engines_table():
    assert cli.ENGINES == {"loop": run, "graph": run_graph}


def test_rules_is_free_and_default_routing():
    assert isinstance(cli.make_model("rules"), RulesModel)


def test_a_model_name_goes_through_langchain(monkeypatch):
    monkeypatch.setenv("OPENAI_BASE_URL", "http://localhost:11434/v1")
    monkeypatch.setenv("PATCHPILOT_PRICE_IN", "0")
    monkeypatch.setenv("PATCHPILOT_PRICE_OUT", "0")
    model = cli.make_model("llama3.1")
    assert isinstance(model, LangChainModel) and model.name == "llama3.1"


def test_openai_raw_prefix_keeps_the_old_path():
    class FakeClient:  # OpenAIModel needs a client or a key; neither reaches the network here
        pass
    model = OpenAIModel("gpt-4o-mini", client=FakeClient())
    assert model.name == "gpt-4o-mini"
    with pytest.raises(RuntimeError, match="OPENAI_API_KEY"):  # no key in tests: the raw path is chosen
        cli.make_model("openai-raw:gpt-4o-mini")


@pytest.mark.parametrize("name", ["gpt-4o-mini", "openai-raw:gpt-4o-mini", "some-unpriced-model"])
def test_model_errors_exit_2_before_any_run(box, name, monkeypatch, capsys):
    called = []
    monkeypatch.setattr(cli, "ENGINES", {"loop": lambda *a: called.append(a), "graph": lambda *a: called.append(a)})
    assert cli.main(["--no-reset", "--repo", str(box), "--model", name]) == 2
    assert called == []
    err = capsys.readouterr().err
    assert "OPENAI_API_KEY" in err or "no price known" in err


@pytest.mark.parametrize("argv, expected", [([], cli.DEFAULT_ENGINE), (["--engine", "graph"], "graph"),
                                            (["--engine", "loop"], "loop")])
def test_engine_flag_picks_the_driver(box, monkeypatch, argv, expected, capsys):
    used = []

    def fake_engine(label):
        def drive(model, ctx, limits, logger):
            used.append((label, model.name))
            logger.log({"event": "stop", "reason": "done"})
            return RunResult(logger.run_id, StopReason.DONE, RunState(), None, str(logger.path), [])
        return drive

    monkeypatch.setattr(cli, "ENGINES", {"loop": fake_engine("loop"), "graph": fake_engine("graph")})
    assert cli.main(["--no-reset", "--repo", str(box), *argv]) == 0
    assert used == [(expected, "rules")]
    assert f"on the {expected} engine" in capsys.readouterr().out


def test_unknown_engine_is_rejected(box):
    with pytest.raises(SystemExit):
        cli.main(["--no-reset", "--repo", str(box), "--engine", "turbo"])
