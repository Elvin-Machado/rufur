"""Task handover packets between agents (plan §10)."""

from __future__ import annotations

import json

from pydantic import BaseModel, Field

from .db.conn import Database, dumps
from .ledger import Ledger
from .textutil import now


class Decision(BaseModel):
    decision: str
    reason: str = ""


class HandoverIn(BaseModel):
    title: str | None = None
    goal: str
    steps_done: list[str] = Field(default_factory=list)
    steps_pending: list[str] = Field(default_factory=list)
    decisions: list[Decision] = Field(default_factory=list)
    open_questions: list[str] = Field(default_factory=list)
    artifacts: list[str] = Field(default_factory=list)
    memory_ids: list[str] = Field(default_factory=list)
    pool: str | None = None


def provenance(db: Database, memory_ids: list[str]) -> list[dict]:
    out = []
    for mid in memory_ids:
        m = db.one(
            "SELECT id, type, text, trust, status, source_type, author_agent, pool_id, sensitivity, valid_from FROM memories WHERE id = ?",
            (mid,),
        )
        if not m:
            continue
        m["sources"] = db.all(
            "SELECT s.quote, e.role, e.agent, e.created_at FROM memory_sources s JOIN events e ON e.id = s.event_id WHERE s.memory_id = ?",
            (mid,),
        )
        out.append(m)
    return out


def save(db: Database, ledger: Ledger, task_id: str, packet: dict, agent: str) -> dict:
    existing = db.one("SELECT * FROM tasks WHERE id = ?", (task_id,))
    t = now()
    if existing:
        db.execute(
            "UPDATE tasks SET title = ?, packet = ?, from_agent = ?, status = 'open', to_agent = NULL, updated_at = ? WHERE id = ?",
            (packet.get("title"), dumps(packet), agent, t, task_id),
        )
    else:
        db.execute(
            "INSERT INTO tasks(id, title, status, from_agent, packet, created_at, updated_at) VALUES(?,?,?,?,?,?,?)",
            (task_id, packet.get("title"), "open", agent, dumps(packet), t, t),
        )
    ledger.audit(agent, "handover.create", task_id, {"memories": len(packet.get("memory_ids", []))})
    ledger.bus.publish("task.updated", id=task_id, status="open", from_agent=agent)
    return get(db, task_id)


def get(db: Database, task_id: str) -> dict | None:
    t = db.one("SELECT * FROM tasks WHERE id = ?", (task_id,))
    if t:
        t["packet"] = json.loads(t["packet"])
    return t


def mark_resumed(db: Database, ledger: Ledger, task_id: str, agent: str) -> None:
    db.execute("UPDATE tasks SET status = 'handed_over', to_agent = ?, updated_at = ? WHERE id = ?", (agent, now(), task_id))
    ledger.audit(agent, "handover.resume", task_id)
    ledger.bus.publish("task.updated", id=task_id, status="handed_over", to_agent=agent)
