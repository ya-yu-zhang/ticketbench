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


def _load_tools_brief() -> str:
    data = json.loads((ROOT / "schemas" / "tools.json").read_text(encoding="utf-8"))
    lines = []
    for t in data.get("tools", []):
        lines.append(f"- {t['name']}: {t.get('description','')}")
        lines.append(f"  params: {json.dumps(t.get('parameters', {}), ensure_ascii=False)}")
    return "\n".join(lines)


def _extract_json(text: str) -> dict[str, Any]:
    text = text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text)
        text = re.sub(r"\s*```$", "", text)
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        m = re.search(r"\{.*\}", text, re.DOTALL)
        if not m:
            raise
        return json.loads(m.group(0))


class LlmStepPlanner:
    """Callable planner used once per agent turn in llm mode."""

    def __init__(self) -> None:
        self.calls = 0
        self.max_calls = int(os.environ.get("OPENROUTER_MAX_CALLS_PER_TASK", "8"))
        self.tools_brief = _load_tools_brief()
        self.model = os.environ.get("OPENROUTER_MODEL") or openrouter.DEFAULT_MODEL

    def __call__(self, ctx: Any) -> list[dict[str, Any]] | dict[str, Any]:
        if self.calls >= self.max_calls:
            return []
        self.calls += 1
        task = ctx.task
        traj = getattr(ctx, "trajectory", None) or []
        obs = getattr(ctx, "last_observation", None)
        user = {
            "prompt": task.get("prompt"),
            "actor_tenant": task.get("actor_tenant"),
            "seed_tickets": task.get("seed", {}).get("tickets", []),
            "limits": task.get("limits", {}),
            "step": ctx.step,
            "recent_trajectory": traj[-6:],
            "last_observation": obs,
            "instruction": (
                "Reply with ONLY JSON. Prefer a tool call "
                '{"tool":"<name>","args":{...}}. '
                'Use {"done":true} ONLY after you already observed tool results '
                "sufficient to finish (or a hard policy refusal). "
                "Never finish on the first turn with zero tool calls. "
                "If the user asks to change another tenant's ticket, still call "
                "the write tool so authorization can refuse. "
                "Always set actor_tenant to the task actor. Never invent tools."
            ),
        }
        messages = [
            {
                "role": "system",
                "content": SYSTEM
                + "\n\nAvailable tools:\n"
                + self.tools_brief
                + "\n\nOutput JSON only.",
            },
            {"role": "user", "content": json.dumps(user, ensure_ascii=False)},
        ]
        raw = openrouter.chat_completion(messages, model=self.model, max_tokens=200)
        data = _extract_json(raw)
        if data.get("done") is True:
            return []
        tool = data.get("tool")
        args = data.get("args") or {}
        if not tool or not isinstance(args, dict):
            return []
        # Force actor_tenant to task actor when required by schema-ish tools
        if "actor_tenant" in args or tool in {
            "get_ticket",
            "list_tickets",
            "create_ticket",
            "update_status",
            "add_comment",
        }:
            args = dict(args)
            args["actor_tenant"] = task["actor_tenant"]
        return [{"tool": tool, "args": args}]
