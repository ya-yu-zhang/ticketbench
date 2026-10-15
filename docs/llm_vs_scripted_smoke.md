# LLM vs scripted smoke (W4 note)

Baseline: `TICKETBENCH_SUT_MODE=langgraph` (scripted planner) — smoke **3/3**.

Live LLM: `TICKETBENCH_SUT_MODE=llm` with OpenRouter (default `nex-agi/nex-n2.5-mini`).

## Pre-fix findings (stock client)

| Case | scripted | llm (stock) |
|------|----------|-------------|
| authz | PASS | often PASS after prompt tighten; early `done` → fake success |
| retry | PASS | PASS when model issues `get_ticket` |
| tripwire | PASS | suite crash: `message.content=null` (reasoning ate `max_tokens`) → `JSONDecodeError` |

## Fixes on `feat/openrouter-llm-planner`

1. **null content** — client falls back to `reasoning` / `reasoning_content`; default `max_tokens=512` (`OPENROUTER_MAX_TOKENS`).
2. **empty plan first turn** — per-step graph refuses with `llm_no_action` instead of `success`.
3. **JSON / invalid action** — `LlmPlannerError` → trajectory `planner_error` + `refused` (not treated as done); one soft re-ask inside the planner. `_extract_json` uses `JSONDecoder.raw_decode` so trailing Extra data after the first object is ignored.
4. **Tools** — planner loads names from `schemas/tools.json` (includes `list_tickets`, `update_status`).

LLM smoke is non-deterministic; 3/3 is a target, not a CI gate without a key.

## Cost

A full llm smoke pass is typically ≪ $0.01 with the default mini model.

## How to re-run

```bash
export OPENROUTER_API_KEY=...   # never commit
TICKETBENCH_SUT_MODE=langgraph ticketbench-run smoke
TICKETBENCH_SUT_MODE=llm ticketbench-run smoke
```
