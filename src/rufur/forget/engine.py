"""Forget on request (plan §9): expand → tombstone → redact → purge → compact → verify."""

from __future__ import annotations

import re

from ..config import Config
from ..db.conn import Database
from ..ledger import Ledger
from ..llm import LLM
from ..textutil import norm_key, sha256
from . import probes, tombstone
from .purge import purge_memories

REDACTED = "[forgotten]"


def expand(db: Database, memory_ids: list[str], include_history: bool = True) -> list[dict]:
    """The targets plus their supersede chain and any session-pool copies of the same claim."""
    out: dict[str, dict] = {}
    queue = list(memory_ids)
    while queue:
        mid = queue.pop()
        if mid in out:
            continue
        m = db.one("SELECT * FROM memories WHERE id = ?", (mid,))
        if not m:
            continue
        out[mid] = m
        if include_history:
            for r in db.all("SELECT id FROM memories WHERE superseded_by = ?", (mid,)):
                queue.append(r["id"])
            if m["superseded_by"]:
                queue.append(m["superseded_by"])
        # Staged copies of the same claim in any session pool.
        for r in db.all(
            "SELECT m.id FROM memories m JOIN pools p ON p.id = m.pool_id WHERE p.kind = 'session' AND m.text = ?",
            (m["text"],),
        ):
            queue.append(r["id"])
    return list(out.values())


def key_strings(db: Database, mems: list[dict]) -> list[str]:
    ids = [m["id"] for m in mems]
    marks = ",".join("?" * len(ids))
    strings: set[str] = set()
    for m in mems:
        strings.add(m["text"])
    for r in db.all(f"SELECT quote FROM memory_sources WHERE memory_id IN ({marks})", tuple(ids)):
        if len(r["quote"]) >= 4:
            strings.add(r["quote"])
    # The bare value ("Berlin") is the most likely thing to linger in token indexes, but only scan
    # for it if no surviving memory legitimately contains it.
    for m in mems:
        v = m["value"].strip()
        if len(v) >= 4:
            still_used = db.scalar(
                f"SELECT 1 FROM memories WHERE id NOT IN ({marks}) AND lower(text) LIKE ? LIMIT 1",
                (*ids, f"%{v.lower()}%"),
            )
            if not still_used:
                strings.add(v)
    return sorted(strings, key=len, reverse=True)


def redact_events(db: Database, mems: list[dict], strings: list[str]) -> int:
    ids = [m["id"] for m in mems]
    marks = ",".join("?" * len(ids))
    event_ids = {r["event_id"] for r in db.all(f"SELECT event_id FROM memory_sources WHERE memory_id IN ({marks})", tuple(ids))}
    # Also any event that mentions the content without being a recorded source.
    for s in strings:
        for r in db.all("SELECT id FROM events WHERE lower(content) LIKE ?", (f"%{s.lower()}%",)):
            event_ids.add(r["id"])
    patterns = [re.compile(re.escape(s).replace(r"\ ", r"\s+"), re.I) for s in strings]
    n = 0
    for eid in event_ids:
        ev = db.one("SELECT content FROM events WHERE id = ?", (eid,))
        if not ev:
            continue
        content = ev["content"]
        for p in patterns:
            content = p.sub(REDACTED, content)
        if content != ev["content"]:
            db.execute("UPDATE events SET content = ?, content_hash = ?, redacted = 1 WHERE id = ?", (content, sha256(content), eid))
            n += 1
    return n


def forget(cfg: Config, db: Database, ledger: Ledger, embedder, llm: LLM, memory_ids: list[str], agent: str,
           include_history: bool = True, verify: bool = True) -> dict:  # fmt: skip
    mems = expand(db, memory_ids, include_history)
    # Daily journals that summarised these memories go too; their text may contain the fact.
    ids0 = [m["id"] for m in mems]
    if ids0:
        marks = ",".join("?" * len(ids0))
        for r in db.all(f"SELECT DISTINCT child_id FROM lineage WHERE child_kind = 'journal' AND memory_id IN ({marks})", tuple(ids0)):
            if r["child_id"] not in ids0 and (j := db.one("SELECT * FROM memories WHERE id = ?", (r["child_id"],))):
                mems.append(j)
    if not mems:
        return {"forgotten": [], "report": None, "error": "no matching memories"}
    ids = [m["id"] for m in mems]
    strings = key_strings(db, mems)

    tombstones = sum(tombstone.add(db, m) for m in mems)
    redacted = redact_events(db, mems, strings)
    counts = purge_memories(db, ids)
    # Ops and audit rows already carry no content; tombstone the ops so sync replicas apply the delete first.
    for mid in ids:
        ledger.op("delete", mid, agent, {})
    ledger.audit(agent, "forget", ",".join(ids), {"count": len(ids), "redacted_events": redacted})
    db.compact()
    for mid in ids:
        ledger.bus.publish("memory.deleted", id=mid)

    report = probes.verify(cfg, db, embedder, llm, ids, mems, strings) if verify else None
    return {
        "forgotten": [{"id": m["id"], "type": m["type"], "subject_key": sha256(norm_key(m["subject"]))[:12]} for m in mems],
        "purged": counts,
        "redacted_events": redacted,
        "tombstones": tombstones,
        "report": report,
    }
