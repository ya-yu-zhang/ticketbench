"""Thin LangGraph-style SUT with deterministic scripted planner for smoke/CI."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable

from sut.tools.authz import UnauthorizedError
from sut.tools.tickets import TicketStore, TransientError


Planner = Callable[["RunContext"], list[dict[str, Any]] | dict[str, Any]]


@dataclass
class RunContext:
    task: dict[str, Any]
    store: TicketStore
    step: int = 0
    retries: int = 0


@dataclass
class TrajectoryEvent:
    type: str
    data: dict[str, Any] = field(default_factory=dict)


@dataclass
class RunResult:
    final: dict[str, Any]
    trajectory: list[TrajectoryEvent]
    ticket_snapshot: dict[str, Any]


def default_scripted_planner(ctx: RunContext) -> list[dict[str, Any]] | dict[str, Any]:
    """Deterministic tool plans keyed by task id / labels — no live LLM required."""
    tid = ctx.task.get("id", "")
    actor = ctx.task["actor_tenant"]
    seed = ctx.task.get("seed", {}).get("tickets", [])
    foreign = next((t for t in seed if t.get("tenant_id") != actor), None)

    if "authz" in tid or ctx.task.get("expected_final", {}).get("refusal_reason") == "unauthorized":
        target = foreign["id"] if foreign else "TK-UNKNOWN"
        # Bad agent attempt: try to update foreign ticket, then refuse path is via tool authz.
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

    if "tripwire" in tid or ctx.task.get("expected_trajectory", {}).get("expect_tripwire"):
        # Keep requesting a harmless list to burn steps until tripwire.
        return {
            "loop_tool": {
                "tool": "list_tickets",
                "args": {"actor_tenant": actor},
            }
        }

    if "retry" in tid or ctx.task.get("expected_trajectory", {}).get("expect_retry_on"):
        own = next((t for t in seed if t.get("tenant_id") == actor), None)
        ticket_id = own["id"] if own else f"TK-{actor}-1"
        return [
            {
                "tool": "get_ticket",
                "args": {"ticket_id": ticket_id, "actor_tenant": actor},
            }
        ]

    # Benign success: list own tickets.
    return [{"tool": "list_tickets", "args": {"actor_tenant": actor}}]


def run_sut(
    task: dict[str, Any],
    db_path: str,
    planner: Planner | None = None,
) -> RunResult:
    planner = planner or default_scripted_planner
    limits = task.get("limits", {})
    max_steps = int(limits.get("max_steps", 8))
    max_retries = int(limits.get("max_retries", 2))
    inject = set()
    if task.get("expected_trajectory", {}).get("expect_retry_on"):
        inject.add("get_ticket")

    store = TicketStore(db_path=__import__("pathlib").Path(db_path), fail_once_tools=inject)
    try:
        store.seed(task.get("seed", {}).get("tickets", []))
        traj: list[TrajectoryEvent] = []
        ctx = RunContext(task=task, store=store)
        plan = planner(ctx)

        refusal_reason = None
        status = "success"
        unauthorized_hit = False

        # Looping plan for tripwire demos.
        if isinstance(plan, dict) and "loop_tool" in plan:
            call = plan["loop_tool"]
            while True:
                if ctx.step >= max_steps:
                    refusal_reason = "max_steps"
                    status = "refused"
                    traj.append(
                        TrajectoryEvent("tripwire", {"reason": "max_steps", "step": ctx.step})
                    )
                    break
                ctx.step += 1
                traj.append(TrajectoryEvent("tool_call", {"tool": call["tool"], "args": call["args"], "step": ctx.step}))
                try:
                    result = store.dispatch(call["tool"], call["args"])
                    traj.append(TrajectoryEvent("tool_result", {"ok": True, "result": result}))
                except TransientError as e:
                    traj.append(TrajectoryEvent("tool_error", {"error": "TransientError", "msg": str(e)}))
                    ctx.retries += 1
                    if ctx.retries > max_retries:
                        status = "refused"
                        refusal_reason = "retry_exhausted"
                        break
                    traj.append(TrajectoryEvent("retry", {"attempt": ctx.retries}))
                except UnauthorizedError as e:
                    unauthorized_hit = True
                    traj.append(TrajectoryEvent("unauthorized", {"msg": str(e)}))
                    status = "refused"
                    refusal_reason = "unauthorized"
                    break
            final = {
                "status": status,
                "ticket_state": store.snapshot(),
                "refusal_reason": refusal_reason,
            }
            return RunResult(final=final, trajectory=traj, ticket_snapshot=store.snapshot())

        # Finite planned calls.
        assert isinstance(plan, list)
        for call in plan:
            if ctx.step >= max_steps:
                status = "refused"
                refusal_reason = "max_steps"
                traj.append(TrajectoryEvent("tripwire", {"reason": "max_steps", "step": ctx.step}))
                break
            ctx.step += 1
            traj.append(
                TrajectoryEvent("tool_call", {"tool": call["tool"], "args": call["args"], "step": ctx.step})
            )
            while True:
                try:
                    result = store.dispatch(call["tool"], call["args"])
                    traj.append(TrajectoryEvent("tool_result", {"ok": True, "result": result}))
                    break
                except TransientError as e:
                    traj.append(TrajectoryEvent("tool_error", {"error": "TransientError", "msg": str(e)}))
                    ctx.retries += 1
                    if ctx.retries > max_retries:
                        status = "refused"
                        refusal_reason = "retry_exhausted"
                        break
                    traj.append(TrajectoryEvent("retry", {"attempt": ctx.retries}))
                    continue
                except UnauthorizedError as e:
                    unauthorized_hit = True
                    traj.append(TrajectoryEvent("unauthorized", {"msg": str(e)}))
                    status = "refused"
                    refusal_reason = "unauthorized"
                    break
            if status == "refused":
                break

        if unauthorized_hit:
            status = "refused"
            refusal_reason = "unauthorized"

        final = {
            "status": status,
            "ticket_state": store.snapshot(),
            "refusal_reason": refusal_reason,
        }
        traj.append(TrajectoryEvent("finalize", {"status": status}))
        return RunResult(final=final, trajectory=traj, ticket_snapshot=store.snapshot())
    finally:
        store.close()
