"""Write an eval result as JSON (everything) and Markdown (the tables for the README and the report)."""

from __future__ import annotations

import json
from collections import defaultdict
from pathlib import Path

from agent.model import PRICES_PER_MTOK

REFERENCE_MODEL = "gpt-4o-mini"  # the cost model projects the rules trajectories onto this model's prices


def _pct(v) -> str:
    return "n/a" if v is None else f"{100 * v:.0f}%"


def cost_model(summary: dict, scenarios: int) -> dict:
    p_in, p_out = PRICES_PER_MTOK[REFERENCE_MODEL]
    measured = summary.get("cost_per_task_usd") or 0.0
    tin = summary.get("input_tokens_per_task") or summary.get("est_input_tokens_per_task") or 0
    tout = summary.get("output_tokens_per_task") or summary.get("est_output_tokens_per_task") or 0
    projected = (tin * p_in + tout * p_out) / 1_000_000
    per_task = measured or projected
    return {
        "measured_cost_per_task_usd": round(measured, 6),
        "llm_tokens_per_task": {"input": round(tin), "output": round(tout),
                                "source": "measured" if summary.get("input_tokens_per_task") else "estimated (chars/4)"},
        "projected_cost_per_task_usd": round(projected, 6),
        "reference_model": REFERENCE_MODEL,
        "cost_per_1000_tasks_usd": round(per_task * 1000, 4),
        "cost_at_100x_this_eval_usd": round(per_task * scenarios * 100, 4),
        "cap_per_run_usd": 0.50,
    }


def to_markdown(result: dict) -> str:
    meta, s, h = result["meta"], result["summary"], result["heldout"]
    cm = cost_model(s, meta["scenarios"])
    lines = [
        f"# Eval results: `{meta['label']}`",
        "",
        f"Model `{meta['model']}`, engine `{meta['engine']}`, injection defense "
        f"{'on' if meta['defense'] else 'off'}{', human approves everything' if meta['rubber_stamp'] else ''}; "
        f"{meta['scenarios']} scenarios x {meta['repeats']} repeats = {s['runs']} runs. Generated {meta['generated_at']}. "
        f"Trajectories: `{meta['logs']}/` (git-ignored; re-create with the command below).",
        "",
        "## Summary",
        "",
        "| Metric | All | Held-out |",
        "|---|---|---|",
    ]
    rows = [
        ("Task success (right stop reason and safe path)", "success_rate", _pct),
        ("Stop reason correct", "stop_reason_correct", _pct),
        ("Step accuracy (steps on the reference path)", "step_accuracy", lambda v: f"{v:.2f}"),
        ("Path recall (reference steps covered)", "path_recall", lambda v: f"{v:.2f}"),
        ("Tool-call precision (right tool, right arguments)", "tool_call_precision",
         lambda v: "n/a" if v is None else f"{v:.2f}"),
        ("Wasted steps per task", "wasted_steps_per_task", lambda v: f"{v:.2f}"),
        ("Recovery after one tool failure", "recovery_rate", _pct),
        ("No write before approval", "no_write_before_approval", _pct),
        ("Stop rule never exceeded", "stop_rule_held", _pct),
        ("Steps per task", "steps_per_task", lambda v: f"{v:.2f}"),
        ("p50 / p95 time per task (s)", None, None),
    ]
    for name, key, fmt in rows:
        if key is None:
            lines.append(f"| {name} | {s['p50_time_s']:.2f} / {s['p95_time_s']:.2f} | "
                         f"{h.get('p50_time_s', 0):.2f} / {h.get('p95_time_s', 0):.2f} |")
        else:
            lines.append(f"| {name} | {fmt(s.get(key))} | {fmt(h.get(key)) if h.get('runs') else 'n/a'} |")

    lines += ["", "## By category", "",
              "| Category | Runs | Success | Step acc. | Precision | Wasted | Recovery |", "|---|---|---|---|---|---|---|"]
    for cat, a in result["by_category"].items():
        prec = "n/a" if a["tool_call_precision"] is None else f"{a['tool_call_precision']:.2f}"
        lines.append(f"| {cat} | {a['runs']} | {_pct(a['success_rate'])} | {a['step_accuracy']:.2f} | {prec} | "
                     f"{a['wasted_steps_per_task']:.2f} | {_pct(a['recovery_rate'])} |")

    inj = [r for r in result["runs"] if r["category"] == "injection" and r["repeat"] == 1]
    if inj:
        lines += ["", "## Injection (repeat 1 of each)", "",
                  "| Scenario | Where the text enters | Model tried it | Harm done | Stopped by | Stop reason |",
                  "|---|---|---|---|---|---|"]
        for r in inj:
            lines.append(f"| {r['scenario']} | {r['attack_surface']} | {'yes' if r['attack_attempted'] else 'no'} | "
                         f"{'**YES**' if r['harm'] else 'no'} | {r['blocked_by']} | {r['stop_reason']} |")

    per = defaultdict(list)
    for r in result["runs"]:
        per[r["scenario"]].append(r)
    lines += ["", "## Per scenario", "",
              "| Scenario | Split | Expected stop | Got | Pass | Steps | Step acc. | Precision |",
              "|---|---|---|---|---|---|---|---|"]
    for sid, rs in per.items():
        first = rs[0]
        passes = sum(1 for r in rs if r["success"])
        got = sorted({r["stop_reason"] for r in rs})
        prec = sum(r["precise_calls"] for r in rs) / max(1, sum(r["tool_calls"] for r in rs))
        lines.append(f"| {sid} | {first['split']} | {first['expected_stop']} | {', '.join(got)} | {passes}/{len(rs)} | "
                     f"{first['steps']} | {sum(r['step_accuracy'] for r in rs) / len(rs):.2f} | {prec:.2f} |")

    lines += ["", "## Cost model", "",
              "| | Value |", "|---|---|",
              f"| Measured model cost per task | ${cm['measured_cost_per_task_usd']:.4f} |",
              f"| LLM tokens per task ({cm['llm_tokens_per_task']['source']}) | "
              f"{cm['llm_tokens_per_task']['input']} in / {cm['llm_tokens_per_task']['output']} out |",
              f"| Same trajectories on {cm['reference_model']} | ${cm['projected_cost_per_task_usd']:.5f} per task |",
              f"| 1,000 tasks | ${cm['cost_per_1000_tasks_usd']:.2f} |",
              f"| 100x this eval ({meta['scenarios'] * 100} tasks) | ${cm['cost_at_100x_this_eval_usd']:.2f} |",
              f"| Hard cap per run | ${cm['cap_per_run_usd']:.2f} |",
              "", "Prices: `agent/model.py` (USD per 1M tokens). Tokens are an estimate when the model reports none "
              "(the rules model): characters / 4 of exactly what an LLM would have been sent on that trajectory.",
              "", "Reproduce:", "", "```bash",
              f"python run.py eval --model {meta['model']} --engine {meta['engine']} --repeats {meta['repeats']}"
              + ("" if meta["defense"] else " --no-defense") + (" --rubber-stamp" if meta["rubber_stamp"] else ""),
              "```", ""]
    return "\n".join(lines)


def write(result: dict, out_dir: Path) -> tuple[Path, Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    base = out_dir / result["meta"]["label"]
    result = {**result, "cost_model": cost_model(result["summary"], result["meta"]["scenarios"])}
    json_path, md_path = base.with_suffix(".json"), base.with_suffix(".md")
    json_path.write_text(json.dumps(result, indent=2, default=str), encoding="utf-8")
    md_path.write_text(to_markdown(result), encoding="utf-8")
    return json_path, md_path
