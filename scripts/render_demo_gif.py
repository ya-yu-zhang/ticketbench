#!/usr/bin/env python3
"""Render a terminal-style GIF comparing a positive and a negative case trajectory.

Usage:
  python scripts/render_demo_gif.py docs/demo_tripwire.gif \\
      evals/smoke/smoke_tripwire_01.json \\
      evals/holdout/holdout_benign_list_no_tripwire.json
"""

from __future__ import annotations

import argparse
import json
import sys
import tempfile
import textwrap
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from sut.agent.graph import run_sut  # noqa: E402

W, H = 720, 420
BG = (18, 18, 24)
FG = (220, 220, 230)
DIM = (140, 140, 155)
GREEN = (80, 200, 120)
RED = (240, 100, 100)
YELLOW = (240, 200, 80)
CYAN = (100, 200, 220)


def _font(size: int = 14):
    for name in (
        "/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf",
        "/usr/share/fonts/truetype/liberation/LiberationMono-Regular.ttf",
    ):
        p = Path(name)
        if p.exists():
            return ImageFont.truetype(str(p), size)
    return ImageFont.load_default()


def _run(task_path: Path):
    task = json.loads(task_path.read_text(encoding="utf-8"))
    with tempfile.TemporaryDirectory() as td:
        result = run_sut(task, str(Path(td) / "tickets.db"))
    lines = [f"$ demo {task.get('id')}"]
    for ev in result.trajectory:
        t, d = ev.type, ev.data or {}
        if t == "tool_call":
            lines.append(f"→ {d.get('tool')} step={d.get('step')}")
        elif t == "tool_result":
            lines.append("  ✓ tool_result")
        elif t == "tool_error":
            lines.append(f"  ✗ {d.get('error')}")
        elif t == "unauthorized":
            lines.append("  ✗ unauthorized")
        elif t == "retry":
            lines.append(f"  ↻ retry attempt={d.get('attempt')}")
        elif t == "tripwire":
            lines.append(f"  ⛔ tripwire {d.get('reason')}")
        elif t == "finalize":
            lines.append(f"⇒ finalize {d.get('status')}")
        else:
            lines.append(f"  · {t}")
    rr = result.final.get("refusal_reason")
    lines.append(
        f"final={result.final.get('status')}"
        + (f" refusal={rr}" if rr else "")
        + f"  [{result.sut_mode_actual}]"
    )
    return task.get("id", task_path.stem), lines


def _frame(title: str, left: list[str], right: list[str], n: int) -> Image.Image:
    img = Image.new("RGB", (W, H), BG)
    draw = ImageDraw.Draw(img)
    font = _font(13)
    title_font = _font(16)
    draw.text((16, 12), title, fill=CYAN, font=title_font)
    draw.text((16, 40), "NEG / hard path", fill=RED, font=font)
    draw.text((W // 2 + 8, 40), "POS / control", fill=GREEN, font=font)
    draw.line((W // 2, 36, W // 2, H - 12), fill=DIM)
    y0 = 64
    for side, lines, x0 in ((0, left, 16), (1, right, W // 2 + 8)):
        for i, line in enumerate(lines[:n]):
            color = FG
            if "✗" in line or "⛔" in line or "unauthorized" in line or "refused" in line:
                color = RED
            elif "✓" in line or "success" in line:
                color = GREEN
            elif "↻" in line:
                color = YELLOW
            elif line.startswith("$"):
                color = CYAN
            for wrapped in textwrap.wrap(line, width=42) or [line]:
                draw.text((x0, y0 + i * 18), wrapped, fill=color, font=font)
                break
    return img


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("out", type=Path)
    ap.add_argument("neg", type=Path, help="hard-path / failure-demo task JSON")
    ap.add_argument("pos", type=Path, help="control / success task JSON")
    args = ap.parse_args()
    neg_id, neg_lines = _run(args.neg)
    pos_id, pos_lines = _run(args.pos)
    title = f"ticketbench · {neg_id}  vs  {pos_id}"
    max_n = max(len(neg_lines), len(pos_lines))
    frames = [_frame(title, neg_lines, pos_lines, k) for k in range(1, max_n + 1)]
    # hold last frame
    frames.extend([frames[-1]] * 4)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    frames[0].save(
        args.out,
        save_all=True,
        append_images=frames[1:],
        duration=450,
        loop=0,
        optimize=True,
    )
    print(f"wrote {args.out} ({args.out.stat().st_size} bytes, {len(frames)} frames)")


if __name__ == "__main__":
    main()
