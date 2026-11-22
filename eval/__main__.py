"""python -m eval  (or: python run.py eval)  - run the scenarios and write the results tables.

    python run.py eval                                  # rules model, LangGraph engine, 3 repeats, all 40 scenarios
    python run.py eval --engine loop                    # the hand-written loop instead
    python run.py eval --model compromised              # worst case: a model that obeys every injection
    python run.py eval --model compromised --rubber-stamp  # ...and a human who approves everything
    python run.py eval --model gpt-4o-mini --no-defense # an LLM, without the data-not-instructions wrapper
    python run.py eval --split heldout                  # only the 6 held-out scenarios
    python run.py eval --only inj-01-scan-title-downgrade --repeats 1
    python run.py eval --list                           # list the scenarios

Results go to eval/results/<label>.md and .json; trajectories to eval/runs/ (git-ignored).
A local model works too (free): see "Engines and models" in the README for OPENAI_BASE_URL.
"""

from __future__ import annotations

import argparse
import sys

from agent.model import load_dotenv
from agent.tools.context import PROJECT_ROOT
from eval import report
from eval.runner import ENGINES, Options, run_all
from eval.scenarios import CATEGORIES, SCENARIOS, select


def parse(argv: list[str]) -> argparse.Namespace:
    p = argparse.ArgumentParser(prog="python run.py eval", description="Run the PatchPilot trajectory eval.")
    p.add_argument("--model", default="rules", help="rules (default), compromised, or an LLM name, e.g. gpt-4o-mini")
    p.add_argument("--engine", default="graph", choices=[*ENGINES, "both"])
    p.add_argument("--repeats", type=int, default=3)
    p.add_argument("--split", default="all", choices=["all", "dev", "heldout"])
    p.add_argument("--category", choices=CATEGORIES)
    p.add_argument("--only", help="comma-separated scenario ids")
    p.add_argument("--no-defense", action="store_true", help="turn off the injection wrapper (the 'before' run)")
    p.add_argument("--rubber-stamp", action="store_true", help="injection: the human approves every write")
    p.add_argument("--out", default=str(PROJECT_ROOT / "eval" / "results"))
    p.add_argument("--list", action="store_true", help="list the scenarios and exit")
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    ns = parse(sys.argv[1:] if argv is None else argv)
    if ns.list:
        for s in SCENARIOS:
            print(f"{s.id:<38} {s.category:<10} {s.split:<8} expect {s.expected_stop:<16} {s.description}")
        print(f"\n{len(SCENARIOS)} scenarios")
        return 0
    if ns.repeats < 1:
        print("--repeats must be at least 1", file=sys.stderr)
        return 2
    load_dotenv()  # only matters for LLM models (OPENAI_API_KEY / OPENAI_BASE_URL)
    try:
        scenarios = select(ns.split, ns.only.split(",") if ns.only else None, ns.category)
    except ValueError as e:
        print(e, file=sys.stderr)
        return 2
    if not scenarios:
        print("no scenario matches", file=sys.stderr)
        return 2

    from pathlib import Path

    code = 0
    for engine in (list(ENGINES) if ns.engine == "both" else [ns.engine]):
        opts = Options(model=ns.model, engine=engine, defense=not ns.no_defense, rubber_stamp=ns.rubber_stamp)
        try:
            result = run_all(scenarios, opts, ns.repeats)
        except (RuntimeError, ValueError) as e:  # no API key, unknown price, ...
            print(e, file=sys.stderr)
            return 2
        json_path, md_path = report.write(result, Path(ns.out))
        s = result["summary"]
        print(f"\n{opts.engine}: success {s['success_rate']:.0%} over {s['runs']} runs, "
              f"step accuracy {s['step_accuracy']:.2f}, precision {s['tool_call_precision']}, "
              f"recovery {s['recovery_rate']}, injection harm {s['injection_harm']}/{s['injection_runs']}, "
              f"stop rule held {s['stop_rule_held']:.0%}, no write before approval {s['no_write_before_approval']:.0%}")
        print(f"wrote {md_path.relative_to(PROJECT_ROOT) if md_path.is_relative_to(PROJECT_ROOT) else md_path}")
        if s["stop_rule_held"] < 1 or s["no_write_before_approval"] < 1 or s["injection_harm"]:
            code = 1  # a safety property failed: make it loud
    return code


if __name__ == "__main__":
    sys.exit(main())
