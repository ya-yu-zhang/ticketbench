"""CLI: run smoke/holdout/tasks/divergence suites."""

from __future__ import annotations

import argparse
import json
import tempfile
from pathlib import Path
from typing import Any

from harness.metrics.assert_task import evaluate
from sut.agent.graph import run_sut
from sut.tools.tickets import load_tool_names


ROOT = Path(__file__).resolve().parents[2]
EVAL_SUITES = {"smoke", "holdout", "divergence"}


def _load_tasks(suite_dir: Path) -> list[dict[str, Any]]:
    tasks = []
    for p in sorted(suite_dir.glob("*.json")):
        tasks.append(json.loads(p.read_text(encoding="utf-8")))
    return tasks


def _serialize_traj(result) -> list[dict[str, Any]]:
    return [{"type": e.type, "data": e.data} for e in result.trajectory]


def run_suite(suite: str) -> dict[str, Any]:
    if suite in EVAL_SUITES:
        suite_dir = ROOT / "evals" / suite
    elif suite == "tasks":
        suite_dir = ROOT / "harness" / "tasks"
    else:
        raise ValueError(f"unknown suite: {suite}")
    tasks = _load_tasks(suite_dir)
    known = load_tool_names(ROOT / "schemas" / "tools.json")
    reports = []
    modes: list[str] = []
    for task in tasks:
        task = dict(task)
        task["_known_tools"] = known
        with tempfile.TemporaryDirectory() as td:
            db = Path(td) / "tickets.db"
            result = run_sut(task, str(db))
            mode_actual = getattr(result, "sut_mode_actual", "unknown")
            modes.append(mode_actual)
            run_blob = {
                "final": result.final,
                "trajectory": _serialize_traj(result),
                "ticket_snapshot": result.ticket_snapshot,
            }
            ev = evaluate(task, run_blob)
            ev["sut_mode_actual"] = mode_actual
            reports.append(ev)
    passed = sum(1 for r in reports if r["pass"])
    n = len(reports)
    final_accuracy = (sum(1 for r in reports if r.get("final_ok")) / n) if n else 0.0
    trajectory_compliance = (
        (sum(1 for r in reports if r.get("trajectory_ok")) / n) if n else 0.0
    )
    return {
        "suite": suite,
        "total": n,
        "passed": passed,
        "failed": n - passed,
        "final_accuracy": final_accuracy,
        "trajectory_compliance": trajectory_compliance,
        "sut_mode_actual": sorted(set(modes)),
        "results": reports,
    }


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="ticketbench-run")
    parser.add_argument(
        "suite",
        choices=["smoke", "holdout", "tasks", "divergence"],
        default="smoke",
        nargs="?",
    )
    parser.add_argument("--out", type=Path, default=None)
    args = parser.parse_args(argv)
    report = run_suite(args.suite)
    text = json.dumps(report, indent=2, ensure_ascii=False)
    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(text, encoding="utf-8")
    print(text)
    if args.suite == "divergence":
        # Demo suite: every case forks AND suite means match exactly.
        cases_ok = bool(report["results"]) and all(
            r.get("final_ok") and not r.get("trajectory_ok") for r in report["results"]
        )
        means_ok = (
            report.get("final_accuracy") == 1.0
            and report.get("trajectory_compliance") == 0.0
        )
        raise SystemExit(0 if (cases_ok and means_ok) else 1)
    if report["failed"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
