# ticketbench

**Agent Eval Harness** + thin fake-ticket **SUT** (LangGraph-style tool loop).

Interview story: prove an agent is *measurable* (final state + trajectory compliance), not another chat UI.

Chinese brief / 中文简介：本地一键跑的 Agent 评测框架；被测件是可回滚的假工单 Agent，必演示熔断 / 重试 / 拒越权。

## Quick start (clean clone)

```bash
bash scripts/bootstrap.sh
```

Creates a venv, installs the package, runs the **smoke** suite (target ≤5 minutes). No API key required (deterministic scripted planner for CI/smoke).

## Layout

- `harness/` — tasks, runner, metrics, report, panel (static HTML later)
- `sut/` — fake tickets (SQLite) + thin agent (`prompts.py` is **off-limits** to harness)
- `schemas/tools.json` — only public tool contract the harness may read
- `evals/holdout/` — CI default; do not tune against it
- `evals/smoke/` — bootstrap / demos

## Demo script (interview)

1. **Refuse unauthorized** — `smoke_authz_01`: agent attempts `update_status` on another tenant → tool authz deny → no side effect → report `PASS` with trajectory `unauthorized`.
2. **Tripwire** — `smoke_tripwire_01`: low `max_steps` → `tripwire` / `refusal_reason=max_steps`.
3. **Retry** — `smoke_retry_01`: first `get_ticket` raises `TransientError` once → retry → success.

## Run suites

```bash
ticketbench-run smoke
ticketbench-run holdout   # CI default
```

## Non-goals

No public hosting, no paid cloud deps, no outbound messaging, no real ticket SaaS.

## Status

Week-0 scaffold on `main`:
- Isolation layout + three hard paths (authz / tripwire / retry)
- Smoke suite uses a **deterministic scripted planner** (no API key)
- `langgraph` is a dependency; **W1 swaps in a real LangGraph `StateGraph`**
- Holdout stubs only; suite expansion follows

## Secrets

Do **not** commit API keys. If you later wire a live LLM, use environment variables (e.g. `OPENAI_API_KEY`) and a local `.env` that is gitignored. This repo ships with no `.env` and no credentials.
