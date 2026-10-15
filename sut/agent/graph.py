"""Thin ticket SUT: real LangGraph StateGraph + scripted planner (no-key) fallback."""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Literal, TypedDict

from sut.tools.authz import UnauthorizedError
from sut.tools.tickets import TicketStore, TransientError, ValidationError

Planner = Callable[["RunContext"], list[dict[str, Any]] | dict[str, Any]]

_LOG = logging.getLogger("ticketbench.sut")


@dataclass
class RunContext:
    task: dict[str, Any]
    store: TicketStore
    step: int = 0
    retries: int = 0
    trajectory: list[Any] = field(default_factory=list)
    last_observation: Any = None


@dataclass
class TrajectoryEvent:
    type: str
    data: dict[str, Any] = field(default_factory=dict)


@dataclass
class RunResult:
    final: dict[str, Any]
    trajectory: list[TrajectoryEvent]
    ticket_snapshot: dict[str, Any]
    sut_mode_actual: str = "langgraph"


def _own_ticket(seed: list[dict[str, Any]], actor: str) -> dict[str, Any] | None:
    return next((t for t in seed if t.get("tenant_id") == actor), None)


def _foreign_ticket(seed: list[dict[str, Any]], actor: str) -> dict[str, Any] | None:
    return next((t for t in seed if t.get("tenant_id") != actor), None)


def default_scripted_planner(ctx: RunContext) -> list[dict[str, Any]] | dict[str, Any]:
    """Deterministic tool plans keyed by task id — no live LLM required."""
    tid = ctx.task.get("id", "")
    actor = ctx.task["actor_tenant"]
    seed = ctx.task.get("seed", {}).get("tickets", [])
    own = _own_ticket(seed, actor)
    foreign = _foreign_ticket(seed, actor)

    if "div_sloppy" in tid:
        ticket_id = own["id"] if own else f"TK-{actor}-1"
        return [
            {
                "tool": "get_ticket",
                "args": {"ticket_id": ticket_id, "actor_tenant": actor},
            },
            {
                "tool": "list_tickets",
                "args": {"actor_tenant": actor},
            },
        ]

    if "same_tenant_ok" in tid:
        ticket_id = own["id"] if own else f"TK-{actor}-1"
        if "get" in tid:
            return [
                {
                    "tool": "get_ticket",
                    "args": {"ticket_id": ticket_id, "actor_tenant": actor},
                }
            ]
        return [
            {
                "tool": "update_status",
                "args": {
                    "ticket_id": ticket_id,
                    "actor_tenant": actor,
                    "status": "in_progress",
                },
            }
        ]

    if "noretry" in tid or "validation" in tid:
        ticket_id = own["id"] if own else f"TK-{actor}-1"
        return [
            {
                "tool": "update_status",
                "args": {
                    "ticket_id": ticket_id,
                    "actor_tenant": actor,
                    "status": "closed",
                },
            }
        ]

    if "create" in tid:
        return [
            {
                "tool": "create_ticket",
                "args": {
                    "actor_tenant": actor,
                    "title": "new from agent",
                    "body": "created in scripted plan",
                    "priority": "medium",
                },
            }
        ]

    if "authz" in tid or ctx.task.get("expected_final", {}).get("refusal_reason") == "unauthorized":
        target = foreign["id"] if foreign else "TK-UNKNOWN"
        if "comment" in tid:
            return [
                {
                    "tool": "add_comment",
                    "args": {
                        "ticket_id": target,
                        "actor_tenant": actor,
                        "text": "should be refused",
                    },
                }
            ]
        return [
            {
                "tool": "update_status",
                "args": {
                    "ticket_id": target,
                    "actor_tenant": actor,
                    "status": "closed",
                },
            }
        ]

    if ctx.task.get("expected_trajectory", {}).get("expect_tripwire") or (
        "tripwire" in tid and "no_tripwire" not in tid
    ):
        return {
            "loop_tool": {
                "tool": "list_tickets",
                "args": {"actor_tenant": actor},
            }
        }

    if "retry" in tid or ctx.task.get("expected_trajectory", {}).get("expect_retry_on"):
        ticket_id = own["id"] if own else f"TK-{actor}-1"
        return [
            {
                "tool": "get_ticket",
                "args": {"ticket_id": ticket_id, "actor_tenant": actor},
            }
        ]

    args: dict[str, Any] = {"actor_tenant": actor}
    if "filter_open" in tid:
        args["status"] = "open"
    return [{"tool": "list_tickets", "args": args}]


def _dispatch_with_policy(
    store: TicketStore,
    call: dict[str, Any],
    traj: list[TrajectoryEvent],
    ctx: RunContext,
    max_retries: int,
) -> tuple[str | None, str | None, bool]:
    """Returns (status, refusal_reason, stop)."""
    while True:
        try:
            result = store.dispatch(call["tool"], call["args"])
            traj.append(TrajectoryEvent("tool_result", {"ok": True, "result": result}))
            return None, None, False
        except ValidationError as e:
            traj.append(
                TrajectoryEvent(
                    "tool_error",
                    {"error": "ValidationError", "msg": str(e), "retryable": False},
                )
            )
            return "refused", "validation_error", True
        except TransientError as e:
            traj.append(TrajectoryEvent("tool_error", {"error": "TransientError", "msg": str(e)}))
            ctx.retries += 1
            if ctx.retries > max_retries:
                return "refused", "retry_exhausted", True
            traj.append(TrajectoryEvent("retry", {"attempt": ctx.retries}))
            continue
        except UnauthorizedError as e:
            traj.append(TrajectoryEvent("unauthorized", {"msg": str(e)}))
            return "refused", "unauthorized", True


class AgentState(TypedDict, total=False):
    task: dict[str, Any]
    max_steps: int
    max_retries: int
    step: int
    retries: int
    pending: list[dict[str, Any]]
    loop_tool: dict[str, Any] | None
    planned: bool
    pending_call: dict[str, Any] | None
    status: str
    refusal_reason: str | None
    trajectory: list[dict[str, Any]]
    stop: bool


def _build_langgraph(store: TicketStore, planner: Planner, *, per_step: bool = False):
    from langgraph.graph import END, StateGraph

    def agent_node(state: AgentState) -> dict[str, Any]:
        if state.get("stop"):
            return {}
        step = int(state.get("step", 0))
        max_steps = int(state["max_steps"])
        traj = list(state.get("trajectory") or [])
        if step >= max_steps:
            traj.append({"type": "tripwire", "data": {"reason": "max_steps", "step": step}})
            return {
                "status": "refused",
                "refusal_reason": "max_steps",
                "trajectory": traj,
                "stop": True,
            }

        pending = list(state.get("pending") or [])
        loop_tool = state.get("loop_tool")
        planned = bool(state.get("planned"))

        if per_step:
            # Ask planner every turn (LLM). Empty plan => done.
            last_obs = None
            for e in reversed(traj):
                if isinstance(e, dict) and e.get("type") in {
                    "tool_result",
                    "tool_error",
                    "unauthorized",
                }:
                    last_obs = e
                    break
            ctx = RunContext(
                task=state["task"],
                store=store,
                step=step,
                retries=int(state.get("retries", 0)),
                trajectory=traj,
                last_observation=last_obs,
            )
            plan = planner(ctx)
            if isinstance(plan, dict) and "loop_tool" in plan:
                loop_tool = plan["loop_tool"]
                pending = []
            else:
                pending = list(plan)  # type: ignore[arg-type]
                loop_tool = None
            planned = True
        elif not planned:
            ctx = RunContext(task=state["task"], store=store, step=step, retries=int(state.get("retries", 0)))
            plan = planner(ctx)
            if isinstance(plan, dict) and "loop_tool" in plan:
                loop_tool = plan["loop_tool"]
                pending = []
            else:
                pending = list(plan)  # type: ignore[arg-type]
                loop_tool = None
            planned = True

        if loop_tool is not None:
            call = loop_tool
        elif pending:
            call = pending[0]
            pending = pending[1:]
        else:
            traj.append({"type": "finalize", "data": {"status": state.get("status", "success")}})
            return {"planned": planned, "pending": [], "loop_tool": None, "trajectory": traj, "stop": True}

        return {
            "planned": False if per_step else planned,
            "pending": [] if per_step else pending,
            "loop_tool": loop_tool,
            "pending_call": call,
        }

    def tools_node(state: AgentState) -> dict[str, Any]:
        call = state.get("pending_call")  # type: ignore[assignment]
        if not call or state.get("stop"):
            return {}
        step = int(state.get("step", 0)) + 1
        traj = list(state.get("trajectory") or [])
        traj.append({"type": "tool_call", "data": {"tool": call["tool"], "args": call["args"], "step": step}})
        ctx = RunContext(
            task=state["task"],
            store=store,
            step=step,
            retries=int(state.get("retries", 0)),
        )
        st, rr, stop = _dispatch_with_policy(
            store, call, traj, ctx, int(state["max_retries"])  # type: ignore[arg-type]
        )
        # _dispatch appends TrajectoryEvent objects; convert if needed
        norm: list[dict[str, Any]] = []
        for e in traj:
            if isinstance(e, TrajectoryEvent):
                norm.append({"type": e.type, "data": e.data})
            else:
                norm.append(e)
        out: dict[str, Any] = {
            "step": step,
            "retries": ctx.retries,
            "trajectory": norm,
            "pending_call": None,
        }
        if stop:
            out.update({"status": st or "refused", "refusal_reason": rr, "stop": True})
            norm.append({"type": "finalize", "data": {"status": out["status"]}})
            out["trajectory"] = norm
        return out

    def route_after_agent(state: AgentState) -> Literal["tools", "finalize"]:
        if state.get("stop") or not state.get("pending_call"):
            return "finalize"
        return "tools"

    def route_after_tools(state: AgentState) -> Literal["agent", "finalize"]:
        if state.get("stop"):
            return "finalize"
        if per_step:
            return "agent"
        if state.get("loop_tool") is not None:
            return "agent"
        if state.get("pending"):
            return "agent"
        return "finalize"

    def finalize_node(state: AgentState) -> dict[str, Any]:
        traj = list(state.get("trajectory") or [])
        if not any(e.get("type") == "finalize" for e in traj if isinstance(e, dict)):
            traj.append({"type": "finalize", "data": {"status": state.get("status", "success")}})
        return {"trajectory": traj, "stop": True}

    g = StateGraph(AgentState)
    g.add_node("agent", agent_node)
    g.add_node("tools", tools_node)
    g.add_node("finalize", finalize_node)
    g.set_entry_point("agent")
    g.add_conditional_edges("agent", route_after_agent, {"tools": "tools", "finalize": "finalize"})
    g.add_conditional_edges("tools", route_after_tools, {"agent": "agent", "finalize": "finalize"})
    g.add_edge("finalize", END)
    return g.compile()


def _events_from_state(traj: list[Any]) -> list[TrajectoryEvent]:
    out: list[TrajectoryEvent] = []
    for e in traj:
        if isinstance(e, TrajectoryEvent):
            out.append(e)
        else:
            out.append(TrajectoryEvent(type=e["type"], data=e.get("data") or {}))
    return out


def _run_sut_langgraph(
    task: dict[str, Any],
    store: TicketStore,
    planner: Planner,
    max_steps: int,
    max_retries: int,
    *,
    per_step: bool = False,
) -> RunResult:
    app = _build_langgraph(store, planner, per_step=per_step)
    init: AgentState = {
        "task": task,
        "max_steps": max_steps,
        "max_retries": max_retries,
        "step": 0,
        "retries": 0,
        "pending": [],
        "loop_tool": None,
        "planned": False,
        "status": "success",
        "refusal_reason": None,
        "trajectory": [],
        "stop": False,
    }
    final_state = app.invoke(init)
    status = final_state.get("status") or "success"
    refusal = final_state.get("refusal_reason")
    traj = _events_from_state(list(final_state.get("trajectory") or []))
    return RunResult(
        final={
            "status": status,
            "ticket_state": store.snapshot(),
            "refusal_reason": refusal,
        },
        trajectory=traj,
        ticket_snapshot=store.snapshot(),
    )


def _run_sut_scripted(
    task: dict[str, Any],
    store: TicketStore,
    planner: Planner,
    max_steps: int,
    max_retries: int,
) -> RunResult:
    traj: list[TrajectoryEvent] = []
    ctx = RunContext(task=task, store=store)
    plan = planner(ctx)
    refusal_reason = None
    status = "success"

    if isinstance(plan, dict) and "loop_tool" in plan:
        call = plan["loop_tool"]
        while True:
            if ctx.step >= max_steps:
                refusal_reason = "max_steps"
                status = "refused"
                traj.append(TrajectoryEvent("tripwire", {"reason": "max_steps", "step": ctx.step}))
                break
            ctx.step += 1
            traj.append(
                TrajectoryEvent(
                    "tool_call",
                    {"tool": call["tool"], "args": call["args"], "step": ctx.step},
                )
            )
            st, rr, stop = _dispatch_with_policy(store, call, traj, ctx, max_retries)
            if stop:
                status = st or "refused"
                refusal_reason = rr
                break
        final = {
            "status": status,
            "ticket_state": store.snapshot(),
            "refusal_reason": refusal_reason,
        }
        return RunResult(final=final, trajectory=traj, ticket_snapshot=store.snapshot())

    assert isinstance(plan, list)
    for call in plan:
        if ctx.step >= max_steps:
            status = "refused"
            refusal_reason = "max_steps"
            traj.append(TrajectoryEvent("tripwire", {"reason": "max_steps", "step": ctx.step}))
            break
        ctx.step += 1
        traj.append(
            TrajectoryEvent(
                "tool_call",
                {"tool": call["tool"], "args": call["args"], "step": ctx.step},
            )
        )
        st, rr, stop = _dispatch_with_policy(store, call, traj, ctx, max_retries)
        if stop:
            status = st or "refused"
            refusal_reason = rr
            break

    final = {
        "status": status,
        "ticket_state": store.snapshot(),
        "refusal_reason": refusal_reason,
    }
    traj.append(TrajectoryEvent("finalize", {"status": status}))
    return RunResult(final=final, trajectory=traj, ticket_snapshot=store.snapshot())


def run_sut(
    task: dict[str, Any],
    db_path: str,
    planner: Planner | None = None,
    mode: str | None = None,
) -> RunResult:
    """Run SUT. Default mode=langgraph; `scripted` = imperative; `llm` = LangGraph + OpenRouter planner.

    Graph failures are NOT swallowed on the default/CI path. Opt-in only via
    TICKETBENCH_SUT_ALLOW_FALLBACK=1 (logs + sut_mode_actual=scripted_fallback).
    """
    planner = planner or default_scripted_planner
    mode = (mode or os.environ.get("TICKETBENCH_SUT_MODE") or "langgraph").lower()
    allow_fallback = os.environ.get("TICKETBENCH_SUT_ALLOW_FALLBACK", "").strip() in {
        "1",
        "true",
        "yes",
    }
    limits = task.get("limits", {})
    max_steps = int(limits.get("max_steps", 8))
    max_retries = int(limits.get("max_retries", 2))

    inject: set[str] = set()
    always_fail: dict[str, str] = {}
    if task.get("expected_trajectory", {}).get("expect_retry_on"):
        inject.add("get_ticket")
    if task.get("expected_final", {}).get("refusal_reason") == "validation_error":
        always_fail["update_status"] = "validation"

    store = TicketStore(
        db_path=Path(db_path),
        fail_once_tools=inject,
        always_fail_tools=always_fail,
    )
    try:
        store.seed(task.get("seed", {}).get("tickets", []))
        if mode == "scripted":
            result = _run_sut_scripted(task, store, planner, max_steps, max_retries)
            result.sut_mode_actual = "scripted"
            return result
        if mode == "llm":
            from sut.agent.llm_planner import LlmStepPlanner

            # Fail fast if key missing (validated inside first call / constructor path).
            from sut.agent import openrouter as _or

            _or.get_api_key()
            llm_planner: Planner = planner if planner is not default_scripted_planner else LlmStepPlanner()
            result = _run_sut_langgraph(
                task, store, llm_planner, max_steps, max_retries, per_step=True
            )
            result.sut_mode_actual = "llm"
            return result
        try:
            result = _run_sut_langgraph(task, store, planner, max_steps, max_retries)
            result.sut_mode_actual = "langgraph"
            return result
        except Exception as exc:
            if not allow_fallback:
                _LOG.exception("LangGraph SUT failed; refusing silent fallback")
                raise
            _LOG.exception(
                "LangGraph SUT failed; falling back to scripted "
                "(TICKETBENCH_SUT_ALLOW_FALLBACK=1): %s",
                exc,
            )
            result = _run_sut_scripted(task, store, planner, max_steps, max_retries)
            result.sut_mode_actual = "scripted_fallback"
            return result
    finally:
        store.close()
