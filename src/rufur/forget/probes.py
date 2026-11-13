"""Deletion verification (plan §9.6): prove a forgotten memory is gone from every store.

Every probe runs twice. Before the delete it must find the content (a positive control: a probe
that can't see the fact proves nothing by coming back empty). After the delete it must find
nothing. Only probes that hit before count towards completeness; the rest are reported as
uninformative instead of being counted as passes.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from pydantic import BaseModel

from ..config import Config
from ..db.conn import Database
from ..llm import LLM, LLMUnavailable
from ..read.recall import fast_recall
from ..read.search import Filter

DIRECT_TABLES = ("memories:id", "memory_sources:memory_id", "lineage:memory_id", "vec_map:memory_id",
                 "file_chunks:memory_id", "edges:memory_id", "typed_rows:memory_id", "pool_members:memory_id")  # fmt: skip

TEXT_STORES = {
    "ledger": "SELECT count(*) FROM memories WHERE lower(text) LIKE ? OR lower(value) LIKE ?",
    "fts": "SELECT count(*) FROM memories_fts WHERE lower(text) LIKE ? OR lower(value) LIKE ?",
    "file_chunks": "SELECT (SELECT count(*) FROM file_chunks WHERE lower(text) LIKE ?) + (SELECT count(*) FROM chunks_fts WHERE lower(text) LIKE ?)",
    "graph": "SELECT count(*) FROM edges WHERE lower(src) LIKE ? OR lower(dst) LIKE ?",
    "typed_rows": "SELECT count(*) FROM typed_rows WHERE lower(row) LIKE ? OR lower(row) LIKE ?",
    "events": "SELECT count(*) FROM events WHERE lower(content) LIKE ? OR lower(content) LIKE ?",
    "handover_packets": "SELECT count(*) FROM tasks WHERE lower(packet) LIKE ? OR lower(title) LIKE ?",
}

# A surviving vector this close to a forgotten one is the same text under another id.
NEAR_IDENTICAL = 0.02


class Paraphrases(BaseModel):
    questions: list[str]


PROBE_SYSTEM = """For each fact, write 2 different questions a user might ask whose answer is that fact.
Make one of them indirect, without reusing the fact's key words. Return {"questions": [...]}."""


@dataclass
class Probe:
    store: str
    kind: str  # direct | text | recall | vector | bytes
    run: Callable[[], list]
    # Positive by construction: skip the before-run (the bytes were certainly in the file while the row existed).
    by_construction: bool = False
    before: list = field(default_factory=list)
    after: list = field(default_factory=list)

    @property
    def informative(self) -> bool:
        return self.by_construction or bool(self.before)

    def summary(self) -> dict:
        """Receipt-safe: counts only, no queries or matched text."""
        return {"store": self.store, "kind": self.kind, "informative": self.informative,
                "before_hits": None if self.by_construction else len(self.before), "after_hits": len(self.after)}  # fmt: skip


def make_queries(llm: LLM, memories: list[dict]) -> tuple[list[str], int]:
    queries: list[str] = []
    for m in memories:
        queries.append(m["text"])
        if m["subject"] and m["predicate"]:
            queries.append(f"What is the {m['predicate']} of {m['subject']}?")
    tokens = 0
    try:
        # One call for all targets; per-memory calls made forgetting a batch cost seconds per item.
        facts = "\n".join(f"- {m['text']}" for m in memories[:8])
        out, usage = llm.complete("probe", PROBE_SYSTEM, facts, Paraphrases)
        queries.extend(out.questions[: 2 * len(memories[:8])])
        tokens = usage.tokens_in + usage.tokens_out
    except LLMUnavailable:
        pass
    return list(dict.fromkeys(queries)), tokens


def stored_vectors(db: Database, memory_ids: list[str]) -> dict[str, bytes]:
    """The targets' own embeddings, held in RAM only for the after-probe, never persisted."""
    marks = ",".join("?" * len(memory_ids))
    out = {}
    for r in db.all(f"SELECT rowid, memory_id FROM vec_map WHERE kind = 'memory' AND memory_id IN ({marks})", tuple(memory_ids)):
        blob = db.scalar("SELECT embedding FROM vec_items WHERE rowid = ?", (r["rowid"],))
        if blob:
            out[r["memory_id"]] = blob
    return out


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


def build(cfg: Config, db: Database, embedder, llm: LLM, memory_ids: list[str], forgotten: list[dict],
          key_strings: list[str]) -> tuple[list[Probe], list[str], int]:  # fmt: skip
    """Returns (probes, recall queries, llm tokens). Must be called before anything is deleted."""
    probes: list[Probe] = []
    marks = ",".join("?" * len(memory_ids))
    ids = tuple(memory_ids)

    # 1. Direct lookup by id in every table that can hold one, plus id references inside JSON columns.
    for spec in DIRECT_TABLES:
        table, col = spec.split(":")
        probes.append(Probe(table, "direct", lambda t=table, c=col: [
            f"{t}: {n}" for n in [db.scalar(f"SELECT count(*) FROM {t} WHERE {c} IN ({marks})", ids)] if n]))  # fmt: skip
    for table, col in (("conflicts", "memory_ids"), ("tasks", "packet")):
        probes.append(Probe(table, "direct", lambda t=table, c=col: [
            mid for mid in memory_ids if db.scalar(f"SELECT 1 FROM {t} WHERE {c} LIKE ? LIMIT 1", (f"%{mid}%",))]))  # fmt: skip

    # 2. Content scans of each derived store.
    def text_scan(sql: str) -> list:
        hits = 0
        for s in key_strings:
            like = f"%{s.lower()}%"
            hits += db.scalar(sql, (like, like)) or 0
        return [hits] if hits else []

    for store, sql in TEXT_STORES.items():
        probes.append(Probe(store, "text", lambda q=sql: text_scan(q)))

    # 3. Recall probes through the read path with the widest filter: covers recall, recall_past and
    #    every pool, all statuses, all clearances.
    queries, tokens = make_queries(llm, forgotten)
    all_pools = [r["id"] for r in db.all("SELECT id FROM pools")]
    flt = Filter(pool_ids=all_pools, clearance="restricted", min_trust="low",
                 statuses=("active", "disputed", "quarantined", "superseded", "archived"), current_only=False)  # fmt: skip
    lowered = [s.lower() for s in key_strings]

    def recall_hits(q: str) -> list:
        res = fast_recall(db, embedder, q, flt, token_budget=4000, threshold=0.0,
                          half_life_days=cfg.recency_half_life_days, k=cfg.candidate_k)  # fmt: skip
        return [it["id"] for it in res.items
                if it["id"] in memory_ids or any(s in f"{it['text']} {it['body']}".lower() for s in lowered)]  # fmt: skip

    for q in queries:
        probes.append(Probe("recall", "recall", lambda q=q: recall_hits(q)))

    # 4. Nearest-neighbour search with each target's own embedding: catches a copy under another id
    #    that the text probes would miss if its wording differs only in whitespace or case.
    for mid, blob in stored_vectors(db, memory_ids).items():
        probes.append(Probe("vector", "vector", lambda b=blob: [
            r["memory_id"] for r in db.all(
                "WITH knn AS (SELECT rowid, distance FROM vec_items WHERE embedding MATCH ? AND k = 5) "
                "SELECT vm.memory_id, knn.distance FROM knn JOIN vec_map vm ON vm.rowid = knn.rowid", (b,))
            if r["distance"] <= NEAR_IDENTICAL]))  # fmt: skip

    # 5. Raw byte scan of the database, its WAL, and every derived file.
    paths = [cfg.db_path, Path(f"{cfg.db_path}-wal"), *cfg.files_dir.rglob("*")]
    probes.append(Probe("byte_scan", "bytes", lambda: _byte_scan(paths, key_strings), by_construction=True))
    return probes, queries, tokens


def run(probes: list[Probe], phase: str) -> None:
    for p in probes:
        if phase == "before" and p.by_construction:
            continue
        setattr(p, phase, p.run())


def report(probes: list[Probe], memory_ids: list[str], queries: list[str], tokens: int) -> dict:
    stores: dict[str, dict] = {}
    for p in probes:
        s = stores.setdefault(p.store, {"probes": 0, "informative": 0, "passed": True, "leaks": []})
        s["probes"] += 1
        s["informative"] += p.informative
        if p.after:
            s["passed"] = False
            s["leaks"].extend(p.after)
    informative = [p for p in probes if p.informative]
    passed = sum(1 for p in informative if not p.after)
    return {
        "memory_ids": memory_ids,
        "stores": stores,
        "probes_total": len(probes),
        "probes_informative": len(informative),
        "probes_passed": passed,
        "completeness": round(passed / len(informative), 4) if informative else 0.0,
        "probe_queries": queries,
        "llm_tokens": tokens,
        "note": "Each probe found the content before deletion and must find nothing after; probes that found nothing "
                "before are uninformative and not counted. Facts already inside an agent's context window cannot be "
                "erased there; RUFUR guarantees it stops returning them.",
    }
