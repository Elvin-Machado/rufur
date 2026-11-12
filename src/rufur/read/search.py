"""Hybrid candidate search: FTS5 BM25 + sqlite-vec KNN, fused by reciprocal rank.

Access control, status, validity, trust and sensitivity filters are part of
every SQL query, so a memory the caller may not see never becomes a candidate
(plan §7.2: filter before ranking).
"""

from __future__ import annotations

from dataclasses import dataclass, field

from ..db.conn import Database, pack_vector
from ..embed import EmbedUnavailable
from ..textutil import content_words
from ..write.trust import SENSITIVITY_ORDER, TRUST_ORDER

RRF_K = 60


@dataclass
class Filter:
    pool_ids: list[str]  # pools whose memories are directly visible (long-term, or a subagent's own scratch pool)
    clearance: str = "confidential"
    min_trust: str = "medium_low"
    statuses: tuple[str, ...] = ("active", "disputed")
    current_only: bool = True
    # STM is shared by all of a user's agents, so an STM item is visible only if the long-term pool it
    # will be promoted into is visible too (keeps private pools private across agents).
    stm_pool_ids: list[str] = field(default_factory=list)
    default_target: str = ""
    # Scratch pools see parent memories by reference through pool_members.
    member_pool_ids: list[str] = field(default_factory=list)
    since: float | None = None  # valid at some point at or after
    until: float | None = None  # valid at some point at or before

    @property
    def empty(self) -> bool:
        return not (self.pool_ids or self.stm_pool_ids or self.member_pool_ids)

    def sql(self, alias: str = "m") -> tuple[str, list]:
        def marks(xs) -> str:
            return ",".join("?" * len(xs))

        sens = SENSITIVITY_ORDER[: SENSITIVITY_ORDER.index(self.clearance) + 1]
        trust = TRUST_ORDER[TRUST_ORDER.index(self.min_trust) :]
        visible = (
            f"({alias}.pool_id IN ({marks(self.pool_ids)})"
            f" OR ({alias}.pool_id IN ({marks(self.stm_pool_ids)}) AND COALESCE({alias}.target_pool, ?) IN ({marks(self.pool_ids)}))"
            f" OR {alias}.id IN (SELECT memory_id FROM pool_members WHERE pool_id IN ({marks(self.member_pool_ids)})))"
        )
        params: list = [*self.pool_ids, *self.stm_pool_ids, self.default_target, *self.pool_ids, *self.member_pool_ids]
        parts = [
            visible,
            f"{alias}.status IN ({marks(self.statuses)})",
            f"{alias}.sensitivity IN ({marks(sens)})",
            f"{alias}.trust IN ({marks(trust)})",
        ]
        params += [*self.statuses, *sens, *trust]
        if self.current_only:
            parts.append(f"{alias}.valid_to IS NULL")
        if self.since is not None:
            parts.append(f"({alias}.valid_to IS NULL OR {alias}.valid_to >= ?)")
            params.append(self.since)
        if self.until is not None:
            parts.append(f"{alias}.valid_from <= ?")
            params.append(self.until)
        return " AND ".join(parts), params


@dataclass
class Candidate:
    memory: dict
    rrf: float = 0.0
    cosine: float | None = None
    chunk_text: str | None = None
    hits: list[str] = field(default_factory=list)


def fts_query(text: str) -> str | None:
    words = content_words(text)
    if not words:
        return None
    return " OR ".join(f'"{w}"' for w in dict.fromkeys(words))


def search(db: Database, embedder, query: str, flt: Filter, k: int = 40) -> tuple[list[Candidate], bool]:
    """Returns (candidates ordered by fused rank, whether the vector index was used)."""
    if flt.empty:
        return [], False
    where, params = flt.sql()
    found: dict[str, Candidate] = {}

    def add(mem: dict, rank: int, how: str) -> Candidate:
        c = found.get(mem["id"]) or found.setdefault(mem["id"], Candidate(mem))
        c.rrf += 1.0 / (RRF_K + rank)
        c.hits.append(how)
        return c

    if q := fts_query(query):
        rows = db.all(
            f"""SELECT m.* FROM memories_fts f JOIN memories m ON m.id = f.memory_id
                WHERE memories_fts MATCH ? AND {where} ORDER BY bm25(memories_fts) LIMIT ?""",
            (q, *params, k),
        )
        for rank, m in enumerate(rows):
            add(m, rank, "fts")
        rows = db.all(
            f"""SELECT m.*, f.text AS chunk_text FROM chunks_fts f JOIN memories m ON m.id = f.memory_id
                WHERE chunks_fts MATCH ? AND {where} ORDER BY bm25(chunks_fts) LIMIT ?""",
            (q, *params, k // 2),
        )
        for rank, m in enumerate(rows):
            chunk = m.pop("chunk_text")
            c = add(m, rank, "fts_chunk")
            c.chunk_text = c.chunk_text or chunk

    used_vec = False
    try:
        qvec = embedder.embed([query], kind="query")[0]
        used_vec = True
    except EmbedUnavailable:
        qvec = None
    if qvec is not None:
        rows = db.all(
            f"""WITH knn AS (SELECT rowid, distance FROM vec_items WHERE embedding MATCH ? AND k = ?)
                SELECT m.*, knn.distance, vm.kind AS vkind, fc.text AS chunk_text FROM knn
                JOIN vec_map vm ON vm.rowid = knn.rowid
                JOIN memories m ON m.id = vm.memory_id
                LEFT JOIN file_chunks fc ON vm.kind = 'file_chunk' AND fc.id = vm.item_id
                WHERE {where} ORDER BY knn.distance""",
            (pack_vector(qvec), k * 3, *params),
        )
        seen: set[str] = set()
        for m in rows:
            dist, vkind, chunk = m.pop("distance"), m.pop("vkind"), m.pop("chunk_text")
            if m["id"] in seen:
                continue
            seen.add(m["id"])
            c = add(m, len(seen) - 1, "vector")
            c.cosine = max(c.cosine or -1.0, 1.0 - dist)
            if vkind == "file_chunk":
                c.chunk_text = c.chunk_text or chunk
            if len(seen) >= k:
                break

    return sorted(found.values(), key=lambda c: c.rrf, reverse=True), used_vec
