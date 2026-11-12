"""Scratch memory pools for subagents (plan v2 §4).

A root agent spawns a pool describing what its subagent needs. The pool sees the
matching parent memories *by reference* (pool_members), so later forgets or
updates in the parent reach it, and the subagent gets its own identity that can
read and write only that pool. Closing the pool files the subagent's useful
results back into the parent's short-term memory through the normal write path
(trust, quarantine and conflict checks all apply again) and discards the rest.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

from .db.conn import dumps
from .textutil import new_id, now
from .write.pipeline import ClaimIn

if TYPE_CHECKING:
    from .service import MemoryService


def spawn(svc: "MemoryService", agent: str, purpose: str, needs: list[str], memory_ids: list[str],
          ttl_minutes: int | None, session_id: str | None) -> dict:  # fmt: skip
    parent = svc.agent(agent)
    seeded: list[str] = []
    for need in needs:
        res = svc.recall(agent, need, token_budget=1500, threshold=svc.cfg.abstain_threshold / 2)
        seeded += [it["id"] for it in res["items"]]
    seeded += [mid for mid in memory_ids if svc._safe_readable(agent, mid)]
    seeded = list(dict.fromkeys(seeded))

    pid = new_id("pool")
    sub_name = f"{agent}/sub-{pid[-6:]}"
    expires = now() + 60 * (ttl_minutes or svc.cfg.scratch_ttl_min)
    svc.db.execute(
        "INSERT INTO pools(id, name, kind, owner, description, acl, created_at, parent_pool, owner_agent, owner_session, "
        "expires_at, purpose) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
        (pid, f"scratch:{pid}", "scratch", agent, purpose, dumps({"read": [sub_name], "write": [sub_name]}),
         now(), svc.stm_pool(parent["user_id"])["id"], agent, session_id, expires, purpose),
    )  # fmt: skip
    # The subagent is its own principal (user "scratch:<pool>"), so it can't see the user's STM or LTM,
    # and the parent doesn't see scratch work until pool_close files the useful parts back.
    key = svc.create_agent(sub_name, trust=parent["trust"], clearance=parent["clearance"])["api_key"]
    svc.db.execute("UPDATE agents SET user_id = ? WHERE name = ?", (f"scratch:{pid}", sub_name))
    svc.db.executemany("INSERT OR IGNORE INTO pool_members(pool_id, memory_id) VALUES(?,?)", [(pid, m) for m in seeded])
    svc.ledger.audit(agent, "pool.spawn", pid, {"seeded": len(seeded), "subagent": sub_name})
    svc.bus.publish("pool.spawned", id=pid, agent=sub_name, seeded=len(seeded))
    return {"pool_id": pid, "scoped_key": key, "subagent": sub_name, "seeded": len(seeded), "expires_at": expires,
            "how_to_use": "Give the subagent this key as RUFUR_API_KEY (or Authorization: Bearer) and tell it to "
                          "use RUFUR's recall/remember tools; it only sees this pool. Call pool_close when it's done."}  # fmt: skip


def close(svc: "MemoryService", actor: str, pool_id: str, summary: str | None = None, reason: str = "closed") -> dict:
    p = svc.db.one("SELECT * FROM pools WHERE id = ? AND kind = 'scratch'", (pool_id,))
    if p is None:
        raise LookupError(f"scratch pool {pool_id!r} not found")
    if p["closed_at"]:
        return {"pool_id": pool_id, "already_closed": True}
    sub = svc.db.one("SELECT * FROM agents WHERE user_id = ?", (f"scratch:{pool_id}",))
    allowed = {"sweeper", p["owner_agent"], sub["name"] if sub else None}
    if actor not in allowed:
        raise PermissionError("only the spawning agent or the subagent can close this pool")
    sub_name = sub["name"] if sub else p["owner_agent"]
    parent = svc.agent(p["owner_agent"])
    stm = svc.stm_pool(parent["user_id"])["id"]
    default_target = svc._pool(svc.cfg.default_pool)["id"]

    if summary:
        svc.remember(sub_name, summary, type="event", source="agent", pool=None)  # lands in the scratch pool

    own = svc.db.all("SELECT * FROM memories WHERE pool_id = ? AND status IN ('active', 'disputed')", (pool_id,))
    keep = _keep(svc, own)
    merged = []
    sub_agent = svc.agent(sub_name)
    for m in own:
        if m["id"] not in keep:
            continue
        sources = [(r["event_id"], r["quote"]) for r in svc.db.all(
            "SELECT event_id, quote FROM memory_sources WHERE memory_id = ?", (m["id"],))]  # fmt: skip
        claim = ClaimIn(type=m["type"], text=m["text"], subject=m["subject"], predicate=m["predicate"],
                        value=m["value"], quote=sources[0][1] if sources else m["text"], sensitivity=m["sensitivity"])  # fmt: skip
        res = svc.writer.commit(claim, pool_id=stm, agent=sub_agent, source_type=m["source_type"], sources=sources,
                                tier="stm", target_pool=default_target, candidate_pools=[stm, default_target])  # fmt: skip
        if res.outcome == "created":
            flags = svc.ledger.get_memory(res.memory["id"])["flags"]
            svc.ledger.update_memory(res.memory["id"], flags=sorted(set(flags) | {f"from_pool:{pool_id}"}))
        merged.append(res.summary())

    svc._purge_quietly([m["id"] for m in svc.db.all("SELECT id FROM memories WHERE pool_id = ?", (pool_id,))])
    svc.db.execute("DELETE FROM pool_members WHERE pool_id = ?", (pool_id,))
    svc.db.execute("UPDATE pools SET closed_at = ? WHERE id = ?", (now(), pool_id))
    # The scoped key stops working; the identity row stays so the audit trail still resolves.
    svc.db.execute("UPDATE agents SET key_hash = NULL WHERE name = ?", (sub_name,))
    svc.ledger.audit(actor, "pool.close", pool_id, {"merged": len(merged), "discarded": len(own) - len(keep), "reason": reason})
    svc.bus.publish("pool.closed", id=pool_id, merged=len(merged))
    return {"pool_id": pool_id, "merged": merged, "discarded": len(own) - len(keep), "reason": reason}


def _keep(svc: "MemoryService", own: list[dict]) -> set[str]:
    """Which subagent writes are worth bringing back: the consolidator's store decision, else everything
    except low-importance transient events."""
    if not own:
        return set()
    from .write.consolidate import _ask, validate

    decisions, _tokens = _ask(svc, own, [])
    keep = set()
    for m in own:
        store, _extras, _fell_back = validate(svc, m, decisions.get(m["id"]), set())
        if store != "discard" and not (m["type"] == "event" and m["importance"] < 0.5 and m["id"] not in decisions):
            keep.add(m["id"])
    return keep


def list_pools(svc: "MemoryService", agent: str) -> list[dict]:
    rows = svc.db.all("SELECT * FROM pools WHERE kind = 'scratch' AND (owner_agent = ? OR ? = 'ui') ORDER BY created_at DESC LIMIT 50",
                      (agent, agent))  # fmt: skip
    for r in rows:
        r["acl"] = json.loads(r["acl"])
        r["members"] = svc.db.scalar("SELECT count(*) FROM pool_members WHERE pool_id = ?", (r["id"],))
        r["own_memories"] = svc.db.scalar("SELECT count(*) FROM memories WHERE pool_id = ?", (r["id"],))
    return rows
