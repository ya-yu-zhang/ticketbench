"""SQLite-backed fake ticket store (rollbackable per run)."""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from sut.tools.authz import UnauthorizedError, assert_same_tenant


class TransientError(RuntimeError):
    """Injected flaky tool failure for retry demos."""


@dataclass
class TicketStore:
    db_path: Path
    fail_once_tools: set[str] | None = None

    def __post_init__(self) -> None:
        self.fail_once_tools = set(self.fail_once_tools or [])
        self._failed: set[str] = set()
        self.conn = sqlite3.connect(self.db_path)
        self.conn.row_factory = sqlite3.Row
        self._init_schema()

    def _init_schema(self) -> None:
        self.conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS tickets (
              id TEXT PRIMARY KEY,
              tenant_id TEXT NOT NULL,
              title TEXT NOT NULL,
              body TEXT NOT NULL,
              priority TEXT NOT NULL,
              status TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS comments (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              ticket_id TEXT NOT NULL,
              tenant_id TEXT NOT NULL,
              text TEXT NOT NULL
            );
            """
        )
        self.conn.commit()

    def seed(self, tickets: list[dict[str, Any]]) -> None:
        for t in tickets:
            self.conn.execute(
                "INSERT OR REPLACE INTO tickets(id, tenant_id, title, body, priority, status)"
                " VALUES(?,?,?,?,?,?)",
                (
                    t["id"],
                    t["tenant_id"],
                    t.get("title", t["id"]),
                    t.get("body", ""),
                    t.get("priority", "medium"),
                    t.get("status", "open"),
                ),
            )
        self.conn.commit()

    def snapshot(self) -> dict[str, Any]:
        tickets = [
            dict(r)
            for r in self.conn.execute(
                "SELECT id, tenant_id, title, body, priority, status FROM tickets ORDER BY id"
            )
        ]
        comments = [
            dict(r)
            for r in self.conn.execute(
                "SELECT id, ticket_id, tenant_id, text FROM comments ORDER BY id"
            )
        ]
        return {"tickets": tickets, "comments": comments}

    def _maybe_transient(self, tool: str) -> None:
        if tool in self.fail_once_tools and tool not in self._failed:
            self._failed.add(tool)
            raise TransientError(f"injected failure for {tool}")

    def get_ticket(self, ticket_id: str, actor_tenant: str) -> dict[str, Any]:
        self._maybe_transient("get_ticket")
        row = self.conn.execute(
            "SELECT id, tenant_id, title, body, priority, status FROM tickets WHERE id=?",
            (ticket_id,),
        ).fetchone()
        if row is None:
            raise KeyError(f"unknown ticket {ticket_id}")
        assert_same_tenant(actor_tenant, row["tenant_id"])
        return dict(row)

    def list_tickets(self, actor_tenant: str, status: str | None = None) -> list[dict[str, Any]]:
        self._maybe_transient("list_tickets")
        if status:
            rows = self.conn.execute(
                "SELECT id, tenant_id, title, body, priority, status FROM tickets"
                " WHERE tenant_id=? AND status=? ORDER BY id",
                (actor_tenant, status),
            ).fetchall()
        else:
            rows = self.conn.execute(
                "SELECT id, tenant_id, title, body, priority, status FROM tickets"
                " WHERE tenant_id=? ORDER BY id",
                (actor_tenant,),
            ).fetchall()
        return [dict(r) for r in rows]

    def create_ticket(
        self, actor_tenant: str, title: str, body: str, priority: str
    ) -> dict[str, Any]:
        self._maybe_transient("create_ticket")
        ticket_id = f"TK-{actor_tenant}-{self.conn.total_changes + 1}"
        self.conn.execute(
            "INSERT INTO tickets(id, tenant_id, title, body, priority, status)"
            " VALUES(?,?,?,?,?,?)",
            (ticket_id, actor_tenant, title, body, priority, "open"),
        )
        self.conn.commit()
        return self.get_ticket(ticket_id, actor_tenant)

    def update_status(self, ticket_id: str, actor_tenant: str, status: str) -> dict[str, Any]:
        self._maybe_transient("update_status")
        row = self.conn.execute(
            "SELECT tenant_id FROM tickets WHERE id=?", (ticket_id,)
        ).fetchone()
        if row is None:
            raise KeyError(f"unknown ticket {ticket_id}")
        assert_same_tenant(actor_tenant, row["tenant_id"])
        self.conn.execute(
            "UPDATE tickets SET status=? WHERE id=?", (status, ticket_id)
        )
        self.conn.commit()
        return self.get_ticket(ticket_id, actor_tenant)

    def add_comment(self, ticket_id: str, actor_tenant: str, text: str) -> dict[str, Any]:
        self._maybe_transient("add_comment")
        row = self.conn.execute(
            "SELECT tenant_id FROM tickets WHERE id=?", (ticket_id,)
        ).fetchone()
        if row is None:
            raise KeyError(f"unknown ticket {ticket_id}")
        assert_same_tenant(actor_tenant, row["tenant_id"])
        cur = self.conn.execute(
            "INSERT INTO comments(ticket_id, tenant_id, text) VALUES(?,?,?)",
            (ticket_id, actor_tenant, text),
        )
        self.conn.commit()
        return {"comment_id": cur.lastrowid, "ticket_id": ticket_id, "text": text}

    def dispatch(self, name: str, args: dict[str, Any]) -> Any:
        if name not in {
            "get_ticket",
            "list_tickets",
            "create_ticket",
            "update_status",
            "add_comment",
        }:
            raise ValueError(f"unknown tool {name}")
        return getattr(self, name)(**args)

    def close(self) -> None:
        self.conn.close()


def load_tool_names(schema_path: Path) -> set[str]:
    data = json.loads(schema_path.read_text(encoding="utf-8"))
    return {t["name"] for t in data["tools"]}
