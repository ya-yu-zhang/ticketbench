# ticketbench

**Agent Eval Harness** + thin fake-ticket **SUT** (real LangGraph `StateGraph` + scripted planner fallback).

Interview story: prove an agent is *measurable* (final state + trajectory compliance), not another chat UI.

Chinese brief / 中文简介：本地一键跑的 Agent 评测框架；被测件是可回滚的假工单 Agent，必演示熔断 / 重试 / 拒越权。

## Quick start (clean clone)

```bash
bash scripts/bootstrap.sh
```

Creates a venv, installs the package, runs the **smoke** suite (target ≤5 minutes). No API key required (deterministic scripted planner for CI/smoke/holdout).

## Layout

- `harness/` — tasks, runner, metrics, report, panel (static HTML later)
- `sut/` — fake tickets (SQLite) + thin agent (`prompts.py` is **off-limits** to harness)
- `schemas/tools.json` — only public tool contract the harness may read
- `evals/holdout/` — CI default; do not tune against it (**10** cases on `main`)
- `evals/smoke/` — bootstrap / demos (**3** hard-path demos)
- `evals/divergence/` — metric-fork demos (**2**); not a CI gate

## Demo script (interview)

1. **Refuse unauthorized** — `smoke_authz_01`: agent attempts `update_status` on another tenant → tool authz deny → no side effect → report `PASS` with trajectory `unauthorized`.
2. **Tripwire** — `smoke_tripwire_01`: low `max_steps` → `tripwire` / `refusal_reason=max_steps`.
3. **Retry** — `smoke_retry_01`: first `get_ticket` raises `TransientError` once → retry → success.

Holdout mirrors these with positive/negative pairs (authz refuse, same-tenant OK, tripwire fire vs benign list, retry recover, validation no-retry).

## Run suites

```bash
ticketbench-run smoke
ticketbench-run holdout   # CI default (10/10 on current main)
ticketbench-run tasks     # tunable set (≥20 on current main)
ticketbench-run divergence  # final OK + trajectory FAIL fork demo (not CI)
```

Optional SUT mode:

```bash
TICKETBENCH_SUT_MODE=langgraph ticketbench-run holdout   # default
TICKETBENCH_SUT_MODE=scripted  ticketbench-run holdout   # no-key imperative fallback
```

## Non-goals

No public hosting, no paid cloud deps, no outbound messaging, no real ticket SaaS.

## Status

**W0** closed on isolation + holdout 10/10 + smoke 3/3 (scripted planner).

**W1 (current `main`)**:
- Metrics split: `final_accuracy` = mean(`final_ok`), `trajectory_compliance` = mean(`trajectory_ok`) (independent; overall pass requires both)
- **`harness/tasks`: 20/20** (create / same-tenant / list / authz / tripwire / retry / validation)
- Holdout still **10/10**; smoke **3/3**
- Scripted planner remains the honest no-key mode; real LangGraph `StateGraph` still queued
- Formal `.github/workflows` still needs `workflow` token scope — draft at `harness/ci_draft/ci.yml.example`; static panel in W4

## Secrets

Do **not** commit API keys. If you later wire a live LLM, use environment variables (e.g. `OPENAI_API_KEY`) and a local `.env` that is gitignored. This repo ships with no `.env` and no credentials.
