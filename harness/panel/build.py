"""Build a single-file static HTML trace panel from suite report JSON(s).

Usage:
    ticketbench-run holdout --out harness/report/holdout_last.json
    ticketbench-panel harness/report/holdout_last.json -o harness/report/panel.html

No server, no network, no external assets: open the HTML file directly.
"""

from __future__ import annotations

import argparse
import html
import json
from pathlib import Path
from typing import Any

CSS = """
body{font-family:system-ui,-apple-system,Segoe UI,sans-serif;margin:0;background:#f6f7f9;color:#1d2330}
header{padding:16px 24px;background:#1d2330;color:#fff}
header h1{margin:0;font-size:20px}
header p{margin:4px 0 0;opacity:.75;font-size:13px}
main{padding:16px 24px;max-width:1100px}
.suite{background:#fff;border-radius:8px;margin-bottom:20px;box-shadow:0 1px 2px rgba(0,0,0,.08)}
.suite>h2{margin:0;padding:12px 16px;font-size:16px;border-bottom:1px solid #e5e7eb}
.metrics{display:flex;gap:12px;padding:12px 16px;flex-wrap:wrap}
.metric{background:#f3f4f6;border-radius:6px;padding:8px 12px;min-width:120px}
.metric b{display:block;font-size:20px}
.metric span{font-size:12px;color:#6b7280}
.filters{padding:0 16px 8px;font-size:13px}
details.task{border-top:1px solid #eef0f3}
details.task summary{padding:10px 16px;cursor:pointer;display:flex;gap:10px;align-items:center;flex-wrap:wrap}
.badge{font-size:11px;padding:2px 6px;border-radius:4px;font-weight:600}
.ok{background:#dcfce7;color:#166534}.bad{background:#fee2e2;color:#991b1b}
.lbl{background:#fef3c7;color:#92400e}.mode{background:#e0e7ff;color:#3730a3}
.tid{font-family:ui-monospace,Menlo,monospace;font-size:13px}
ol.steps{margin:0;padding:8px 16px 14px 40px;font-size:13px}
ol.steps li{margin:4px 0}
.ev{font-family:ui-monospace,Menlo,monospace;font-weight:600}
.ev-tool_call{color:#1d4ed8}.ev-tool_result{color:#15803d}.ev-tool_error,.ev-unauthorized{color:#b91c1c}
.ev-retry{color:#a16207}.ev-tripwire{color:#c2410c}.ev-finalize{color:#6b7280}
pre{margin:4px 0 0;background:#f9fafb;padding:6px 8px;border-radius:4px;overflow:auto;max-height:180px;font-size:12px}
.final{padding:0 16px 12px;font-size:13px}
"""

JS = """
document.querySelectorAll('input[data-filter]').forEach(cb=>{
  cb.addEventListener('change',()=>{
    const suite=cb.closest('.suite');const only=cb.checked;
    suite.querySelectorAll('details.task').forEach(d=>{
      d.style.display=(only && d.dataset.pass==='1')?'none':'';
    });
  });
});
"""


def _e(x: Any) -> str:
    return html.escape(str(x), quote=True)


def _pct(x: Any) -> str:
    try:
        return f"{float(x) * 100:.0f}%"
    except (TypeError, ValueError):
        return "n/a"


def _summarize_event(ev: dict[str, Any]) -> str:
    t = ev.get("type", "?")
    d = ev.get("data") or {}
    if t == "tool_call":
        return f"step {_e(d.get('step', '?'))}: <code>{_e(d.get('tool'))}</code>"
    if t == "tool_error":
        return f"{_e(d.get('error'))}: {_e(d.get('msg', ''))}"
    if t == "unauthorized":
        return _e(d.get("msg", "denied"))
    if t == "retry":
        return f"attempt {_e(d.get('attempt'))}"
    if t == "tripwire":
        return f"{_e(d.get('reason'))} at step {_e(d.get('step'))}"
    if t == "finalize":
        return f"status={_e(d.get('status'))}"
    if t == "tool_result":
        return "ok"
    return ""


def _render_task(r: dict[str, Any]) -> str:
    passed = bool(r.get("pass"))
    badges = [
        f'<span class="badge {"ok" if passed else "bad"}">{"PASS" if passed else "FAIL"}</span>',
        f'<span class="badge {"ok" if r.get("final_ok") else "bad"}">final</span>',
        f'<span class="badge {"ok" if r.get("trajectory_ok") else "bad"}">trajectory</span>',
    ]
    for lbl in r.get("labels") or []:
        if lbl != "PASS":
            badges.append(f'<span class="badge lbl">{_e(lbl)}</span>')
    if r.get("sut_mode_actual"):
        badges.append(f'<span class="badge mode">{_e(r["sut_mode_actual"])}</span>')

    steps = []
    for ev in r.get("trajectory") or []:
        t = ev.get("type", "?")
        detail = json.dumps(ev.get("data") or {}, ensure_ascii=False, indent=2)
        steps.append(
            f'<li><span class="ev ev-{_e(t)}">{_e(t)}</span> {_summarize_event(ev)}'
            f"<details><summary>data</summary><pre>{_e(detail)}</pre></details></li>"
        )
    final = r.get("final") or {}
    final_line = (
        f'<div class="final">final status=<b>{_e(final.get("status"))}</b>, '
        f'refusal_reason=<b>{_e(final.get("refusal_reason"))}</b></div>'
    )
    return (
        f'<details class="task" data-pass="{1 if passed else 0}"'
        f'{"" if passed else " open"}><summary><span class="tid">{_e(r.get("task_id"))}</span>'
        f'{"".join(badges)}</summary>{final_line}<ol class="steps">{"".join(steps)}</ol></details>'
    )


def _render_suite(rep: dict[str, Any], idx: int) -> str:
    modes = rep.get("sut_mode_actual")
    mode_txt = ", ".join(modes) if isinstance(modes, list) else (modes or "n/a")
    metrics = [
        ("passed", f'{rep.get("passed", 0)}/{rep.get("total", 0)}'),
        ("final_accuracy", _pct(rep.get("final_accuracy"))),
        ("trajectory_compliance", _pct(rep.get("trajectory_compliance"))),
        ("sut_mode_actual", mode_txt),
    ]
    m_html = "".join(
        f'<div class="metric"><b>{_e(v)}</b><span>{_e(k)}</span></div>' for k, v in metrics
    )
    tasks = "".join(_render_task(r) for r in rep.get("results") or [])
    return (
        f'<section class="suite"><h2>suite: {_e(rep.get("suite", "?"))}</h2>'
        f'<div class="metrics">{m_html}</div>'
        f'<div class="filters"><label><input type="checkbox" data-filter id="f{idx}"> '
        f"only show failures</label></div>{tasks}</section>"
    )


def build_html(reports: list[dict[str, Any]]) -> str:
    body = "".join(_render_suite(r, i) for i, r in enumerate(reports))
    return (
        "<!doctype html><html lang=\"en\"><head><meta charset=\"utf-8\">"
        "<meta name=\"viewport\" content=\"width=device-width,initial-scale=1\">"
        f"<title>ticketbench trace panel</title><style>{CSS}</style></head><body>"
        "<header><h1>ticketbench trace panel</h1>"
        "<p>Per-step tool calls, F_* labels, and final vs trajectory metrics. "
        "Static file; no server or network.</p></header>"
        f"<main>{body}</main><script>{JS}</script></body></html>\n"
    )


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(prog="ticketbench-panel")
    ap.add_argument("reports", nargs="+", type=Path, help="suite report JSON(s) from ticketbench-run --out")
    ap.add_argument("-o", "--out", type=Path, default=Path("harness/report/panel.html"))
    args = ap.parse_args(argv)
    reports = [json.loads(p.read_text(encoding="utf-8")) for p in args.reports]
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(build_html(reports), encoding="utf-8")
    print(f"wrote {args.out} ({len(reports)} suite(s))")


if __name__ == "__main__":
    main()
