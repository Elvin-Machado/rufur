"""Deletion verification (plan §9.6): prove a forgotten memory is gone from every store."""

from __future__ import annotations

from pathlib import Path

from pydantic import BaseModel

from ..config import Config
from ..db.conn import Database
from ..llm import LLM, LLMUnavailable
from ..read.recall import fast_recall
from ..read.search import Filter

DIRECT_TABLES = ("memories:id", "memory_sources:memory_id", "lineage:memory_id", "vec_map:memory_id",
                 "file_chunks:memory_id", "edges:memory_id", "typed_rows:memory_id")  # fmt: skip

TEXT_STORES = {
    "ledger": "SELECT count(*) FROM memories WHERE lower(text) LIKE ? OR lower(value) LIKE ?",
    "fts": "SELECT count(*) FROM memories_fts WHERE lower(text) LIKE ? OR lower(value) LIKE ?",
    "file_chunks": "SELECT (SELECT count(*) FROM file_chunks WHERE lower(text) LIKE ?) + (SELECT count(*) FROM chunks_fts WHERE lower(text) LIKE ?)",
    "graph": "SELECT count(*) FROM edges WHERE lower(src) LIKE ? OR lower(dst) LIKE ?",
    "typed_rows": "SELECT count(*) FROM typed_rows WHERE lower(row) LIKE ? OR lower(row) LIKE ?",
    "events": "SELECT count(*) FROM events WHERE lower(content) LIKE ? OR lower(content) LIKE ?",
}


class Paraphrases(BaseModel):
    questions: list[str]


PROBE_SYSTEM = """Write 3 different questions a user might ask whose answer would be the given fact.
Include one indirect question that does not reuse the fact's key words. Return {"questions": [...]}."""


def make_probes(llm: LLM, memories: list[dict]) -> tuple[list[str], int]:
    queries: list[str] = []
    tokens = 0
    for m in memories:
        queries.append(m["text"])
        if m["subject"] and m["predicate"]:
            queries.append(f"What is the {m['predicate']} of {m['subject']}?")
        try:
            out, usage = llm.complete("probe", PROBE_SYSTEM, m["text"], Paraphrases)
            queries.extend(out.questions[:3])
            tokens += usage.tokens_in + usage.tokens_out
        except LLMUnavailable:
            pass
    return list(dict.fromkeys(queries)), tokens


def _byte_scan(paths: list[Path], needles: list[str]) -> list[str]:
    hits = []
    lowered = [n.lower().encode() for n in needles]
    for p in paths:
        if not p.is_file():
            continue
        data = p.read_bytes().lower()
        for raw, n in zip(needles, lowered):
            if n in data:
                hits.append(f"{p.name}: {len(raw)}-char key string")
    return hits


def verify(cfg: Config, db: Database, embedder, llm: LLM, memory_ids: list[str], forgotten: list[dict],
           key_strings: list[str]) -> dict:  # fmt: skip
    stores: dict[str, dict] = {}

    # 1. Direct lookup by id in every table that can hold one.
    direct_hits = []
    for spec in DIRECT_TABLES:
        table, col = spec.split(":")
        marks = ",".join("?" * len(memory_ids))
        n = db.scalar(f"SELECT count(*) FROM {table} WHERE {col} IN ({marks})", tuple(memory_ids))
        if n:
            direct_hits.append(f"{table}: {n}")
    stores["direct_lookup"] = {"passed": not direct_hits, "detail": direct_hits}

    # 2. Content scans of each derived store.
    for store, sql in TEXT_STORES.items():
        hits = 0
        for s in key_strings:
            like = f"%{s.lower()}%"
            hits += db.scalar(sql, (like, like)) or 0
        stores[store] = {"passed": hits == 0, "detail": f"{hits} matching rows"}

    # 3. Recall probes (direct, paraphrase, indirect) through the read path with the widest filter.
    queries, probe_tokens = make_probes(llm, forgotten)
    all_pools = [r["id"] for r in db.all("SELECT id FROM pools")]
    flt = Filter(pool_ids=all_pools, clearance="restricted", min_trust="low",
                 statuses=("active", "disputed", "quarantined", "superseded", "archived"), current_only=False)  # fmt: skip
    leaks = []
    for q in queries:
        res = fast_recall(db, embedder, q, flt, token_budget=4000, threshold=0.0,
                          half_life_days=cfg.recency_half_life_days, k=cfg.candidate_k)  # fmt: skip
        for it in res.items:
            body = f"{it['text']} {it['body']}".lower()
            if it["id"] in memory_ids or any(s.lower() in body for s in key_strings):
                leaks.append({"query": q, "memory_id": it["id"]})
    stores["recall_probes"] = {"passed": not leaks, "detail": {"queries": len(queries), "leaks": leaks}}

    # 4. Raw byte scan of the database, its WAL, and every derived file.
    paths = [cfg.db_path, Path(f"{cfg.db_path}-wal"), *cfg.files_dir.rglob("*")]
    byte_hits = _byte_scan(paths, key_strings)
    stores["byte_scan"] = {"passed": not byte_hits, "detail": byte_hits}

    passed = sum(1 for s in stores.values() if s["passed"])
    return {
        "memory_ids": memory_ids,
        "stores": stores,
        "probes_total": len(stores),
        "probes_passed": passed,
        "completeness": round(passed / len(stores), 4),
        "probe_queries": queries,
        "llm_tokens": probe_tokens,
        "note": "Facts already inside an agent's context window cannot be erased there; RUFUR guarantees it stops returning them.",
    }
