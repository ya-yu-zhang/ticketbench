"""OpenRouter-backed step planner: one tool call per turn, or done."""

from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any

from sut.agent import openrouter
from sut.agent.prompts import SYSTEM

ROOT = Path(__file__).resolve().parents[2]


class LlmPlannerError(RuntimeError):
    """Planner could not produce a valid next action (not the same as done)."""


def _load_tools() -> tuple[str, set[str]]:
    data = json.loads((ROOT / "schemas" / "tools.json").read_text(encoding="utf-8"))
    lines: list[str] = []
    names: set[str] = set()
    for t in data.get("tools", []):
        name = t["name"]
        names.add(name)
        lines.append(f"- {name}: {t.get('description', '')}")
        lines.append(f"  params: {json.dumps(t.get('parameters', {}), ensure_ascii=False)}")
    return "\n".join(lines), names


def _extract_json(text: str) -> dict[str, Any]:
    """Parse the first complete JSON object; tolerate trailing Extra data."""
    text = text.strip()
    if not text:
        raise LlmPlannerError("empty model text; cannot parse JSON action")
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text)
        text = re.sub(r"\s*```$", "", text).strip()
    decoder = json.JSONDecoder()
    # Prefer raw_decode from the first '{': survives trailing prose / second blobs.
    start = text.find("{")
    if start < 0:
        raise LlmPlannerError(f"model output is not JSON: {text[:200]!r}")
    try:
        data, _end = decoder.raw_decode(text[start:])
    except json.JSONDecodeError as e:
        # Fallback: non-greedy object slice then raw_decode again
        m = re.search(r"\{[^{}]*(?:\{[^{}]*\}[^{}]*)*\}", text[start:], re.DOTALL)
        if not m:
            raise LlmPlannerError(f"JSON parse failed: {e}") from e
        try:
            data, _end = decoder.raw_decode(m.group(0))
        except json.JSONDecodeError as e2:
            raise LlmPlannerError(f"JSON parse failed: {e2}") from e2
    if not isinstance(data, dict):
        raise LlmPlannerError("model JSON must be an object")
    return data


class LlmStepPlanner:
    """Callable planner used once per agent turn in llm mode."""

    def __init__(self) -> None:
        self.calls = 0
        self.max_calls = int(os.environ.get("OPENROUTER_MAX_CALLS_PER_TASK", "8"))
        self.tools_brief, self.allowed = _load_tools()
        self.model = os.environ.get("OPENROUTER_MODEL") or openrouter.DEFAULT_MODEL

    def __call__(self, ctx: Any) -> list[dict[str, Any]] | dict[str, Any]:
        if self.calls >= self.max_calls:
            return []
        self.calls += 1
        task = ctx.task
        traj = getattr(ctx, "trajectory", None) or []
        obs = getattr(ctx, "last_observation", None)
        allowed_csv = ", ".join(sorted(self.allowed))
        user = {
            "prompt": task.get("prompt"),
            "actor_tenant": task.get("actor_tenant"),
            "seed_tickets": task.get("seed", {}).get("tickets", []),
            "limits": task.get("limits", {}),
            "step": ctx.step,
            "recent_trajectory": traj[-6:],
            "last_observation": obs,
            "instruction": (
                "Reply with ONLY one JSON object.\n"
                'Tool call: {"tool":"<name>","args":{...}}\n'
                'Finish: {"done":true} ONLY after tool results already in trajectory '
                "are enough (or after unauthorized was observed).\n"
                "Never emit done on the first turn with zero tool calls.\n"
                "Cross-tenant close/update: still CALL update_status (or the write tool); "
                "do not invent a verbal refusal JSON — the harness must see the tool call.\n"
                "If asked to keep listing forever: call list_tickets again each turn "
                f"(it IS available). Allowed tools: {allowed_csv}.\n"
                "Always put actor_tenant from the task into args. Never invent tools."
            ),
        }
        messages: list[dict[str, str]] = [
            {
                "role": "system",
                "content": SYSTEM
                + "\n\nAvailable tools:\n"
                + self.tools_brief
                + "\n\nOutput JSON only.",
            },
            {"role": "user", "content": json.dumps(user, ensure_ascii=False)},
        ]
        last_err: Exception | None = None
        raw = ""
        for _attempt in range(2):
            try:
                raw = openrouter.chat_completion(messages, model=self.model)
                data = _extract_json(raw)
                if data.get("done") is True:
                    return []
                tool = data.get("tool")
                args = data.get("args") if isinstance(data.get("args"), dict) else None
                if not tool or args is None:
                    raise LlmPlannerError(f"invalid action object: {data!r}"[:240])
                if tool not in self.allowed:
                    raise LlmPlannerError(
                        f"unknown tool {tool!r}; allowed={sorted(self.allowed)}"
                    )
                out_args = dict(args)
                out_args["actor_tenant"] = task["actor_tenant"]
                return [{"tool": tool, "args": out_args}]
            except LlmPlannerError as e:
                last_err = e
                messages.append({"role": "assistant", "content": raw or ""})
                messages.append(
                    {
                        "role": "user",
                        "content": (
                            f"Invalid previous reply ({e}). "
                            "Reply again with ONLY "
                            '{"tool":"list_tickets","args":{}} '
                            'or {"tool":"update_status","args":{"ticket_id":"...","status":"closed"}} '
                            f"using an allowed tool ({allowed_csv})."
                        ),
                    }
                )
        assert last_err is not None
        raise last_err
