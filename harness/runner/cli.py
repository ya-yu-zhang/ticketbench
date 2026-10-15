"""CLI: run smoke/holdout/tasks suites."""

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


def _load_tasks(suite_dir: Path) -> list[dict[str, Any]]:
    tasks = []
    for p in sorted(suite_dir.glob("*.json")):
        tasks.append(json.loads(p.read_text(encoding="utf-8")))
    return tasks


def _serialize_traj(result) -> list[dict[str, Any]]:
    return [{"type": e.type, "data": e.data} for e in result.trajectory]


def run_suite(suite: str) -> dict[str, Any]:
    suite_dir = ROOT / "evals" / suite if suite in {"smoke", "holdout"} else ROOT / "harness" / "tasks"
    if suite == "tasks":
        suite_dir = ROOT / "harness" / "tasks"
    tasks = _load_tasks(suite_dir)
    known = load_tool_names(ROOT / "schemas" / "tools.json")
    reports = []
    for task in tasks:
        task = dict(task)
        task["_known_tools"] = known
        with tempfile.TemporaryDirectory() as td:
            db = Path(td) / "tickets.db"
            result = run_sut(task, str(db))
            run_blob = {
                "final": result.final,
                "trajectory": _serialize_traj(result),
                "ticket_snapshot": result.ticket_snapshot,
            }
            reports.append(evaluate(task, run_blob))
    passed = sum(1 for r in reports if r["pass"])
    return {
        "suite": suite,
        "total": len(reports),
        "passed": passed,
        "failed": len(reports) - passed,
        "final_accuracy": (passed / len(reports)) if reports else 0.0,
        "trajectory_compliance": (passed / len(reports)) if reports else 0.0,
        "results": reports,
    }


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="ticketbench-run")
    parser.add_argument("suite", choices=["smoke", "holdout", "tasks"], default="smoke", nargs="?")
    parser.add_argument("--out", type=Path, default=None)
    args = parser.parse_args(argv)
    report = run_suite(args.suite)
    text = json.dumps(report, indent=2, ensure_ascii=False)
    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(text, encoding="utf-8")
    print(text)
    if report["failed"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
