"""Thin LangGraph-style SUT with deterministic scripted planner for smoke/CI."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from sut.tools.authz import UnauthorizedError
from sut.tools.tickets import TicketStore, TransientError, ValidationError


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

    # Same-tenant success paths (must precede generic authz/list).
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

    # Permanent validation failure — no retry.
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

    # Create ticket (tunable tasks).
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

    # Cross-tenant authz attempts.
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

    # Tripwire loops.
    if ctx.task.get("expected_trajectory", {}).get("expect_tripwire") or ("tripwire" in tid and "no_tripwire" not in tid):
        return {
            "loop_tool": {
                "tool": "list_tickets",
                "args": {"actor_tenant": actor},
            }
        }

    # Transient retry path.
    if "retry" in tid or ctx.task.get("expected_trajectory", {}).get("expect_retry_on"):
        ticket_id = own["id"] if own else f"TK-{actor}-1"
        return [
            {
                "tool": "get_ticket",
                "args": {"ticket_id": ticket_id, "actor_tenant": actor},
            }
        ]

    # Benign list (optional status filter).
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
    """Returns (status, refusal_reason, stop). status None means continue success path."""
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


def run_sut(
    task: dict[str, Any],
    db_path: str,
    planner: Planner | None = None,
) -> RunResult:
    planner = planner or default_scripted_planner
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
                    traj.append(
                        TrajectoryEvent("tripwire", {"reason": "max_steps", "step": ctx.step})
                    )
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
    finally:
        store.close()
