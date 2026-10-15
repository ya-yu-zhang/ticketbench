#!/usr/bin/env python3
"""Run one task JSON and print a step-by-step trajectory (interview live demo)."""

from __future__ import annotations

import argparse
import json
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from harness.metrics.assert_task import evaluate  # noqa: E402
from sut.agent.graph import run_sut  # noqa: E402
from sut.tools.tickets import load_tool_names  # noqa: E402


def main() -> None:
    ap = argparse.ArgumentParser(description="Run a single ticketbench task with live trace")
    ap.add_argument("task", type=Path, help="path to task JSON")
    args = ap.parse_args()
    task = json.loads(args.task.read_text(encoding="utf-8"))
    task["_known_tools"] = load_tool_names(ROOT / "schemas" / "tools.json")
    with tempfile.TemporaryDirectory() as td:
        result = run_sut(task, str(Path(td) / "tickets.db"))
    print(f"task_id={task.get('id')}  sut_mode_actual={result.sut_mode_actual}")
    for i, ev in enumerate(result.trajectory, 1):
        data = json.dumps(ev.data, ensure_ascii=False)
        print(f"  [{i:02d}] {ev.type}: {data}")
    print(
        f"final status={result.final.get('status')} "
        f"refusal_reason={result.final.get('refusal_reason')}"
    )
    ev = evaluate(
        task,
        {
            "final": result.final,
            "trajectory": [{"type": e.type, "data": e.data} for e in result.trajectory],
            "ticket_snapshot": result.ticket_snapshot,
        },
    )
    print(
        f"assert pass={ev['pass']} final_ok={ev['final_ok']} "
        f"trajectory_ok={ev['trajectory_ok']} labels={ev['labels']}"
    )


if __name__ == "__main__":
    main()
