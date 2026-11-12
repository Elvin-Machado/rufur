"""Hard deletion of memories and every derived item lineage points at."""

from __future__ import annotations

import os

from ..db.conn import Database


def purge_memories(db: Database, memory_ids: list[str], *, cascade_journal: bool = False) -> dict[str, int]:
    """Delete the memories, their derived rows and files. Returns counts per store.

    With `cascade_journal`, daily journal digests that summarised any of these memories are deleted
    too (a user forget must not leave the fact inside a summary). Routine expiry leaves them alone.
    """
    counts = {"ledger": 0, "fts": 0, "vector": 0, "edge": 0, "typed_row": 0, "file_chunk": 0, "file": 0, "sources": 0}
    if not memory_ids:
        return counts
    if cascade_journal:
        marks = ",".join("?" * len(memory_ids))
        journals = {r["child_id"] for r in db.all(
            f"SELECT child_id FROM lineage WHERE child_kind = 'journal' AND memory_id IN ({marks})", tuple(memory_ids))}
        memory_ids = list(dict.fromkeys([*memory_ids, *journals]))
    marks = ",".join("?" * len(memory_ids))
    with db.tx():
        lineage = db.all(f"SELECT child_kind, child_id FROM lineage WHERE memory_id IN ({marks})", tuple(memory_ids))
        for item in lineage:
            kind, cid = item["child_kind"], item["child_id"]
            if kind == "fts":
                db.execute("DELETE FROM memories_fts WHERE rowid = ?", (int(cid),))
            elif kind == "vector":
                db.execute("DELETE FROM vec_items WHERE rowid = ?", (int(cid),))
                db.execute("DELETE FROM vec_map WHERE rowid = ?", (int(cid),))
            elif kind == "edge":
                db.execute("DELETE FROM edges WHERE id = ?", (cid,))
            elif kind == "typed_row":
                db.execute("DELETE FROM typed_rows WHERE id = ?", (cid,))
            elif kind == "file_chunk":
                db.execute("DELETE FROM file_chunks WHERE id = ?", (cid,))
                db.execute("DELETE FROM chunks_fts WHERE chunk_id = ?", (cid,))
            elif kind == "journal":
                pass  # handled by cascade_journal above; the lineage row is dropped below
            elif kind == "file":
                if os.path.exists(cid):
                    os.remove(cid)
            counts[kind] = counts.get(kind, 0) + 1
        # Belt and braces for rows written without lineage (shouldn't happen, but deletion must be complete).
        db.execute(f"DELETE FROM memories_fts WHERE memory_id IN ({marks})", tuple(memory_ids))
        db.execute(f"DELETE FROM chunks_fts WHERE memory_id IN ({marks})", tuple(memory_ids))
        for row in db.all(f"SELECT rowid FROM vec_map WHERE memory_id IN ({marks})", tuple(memory_ids)):
            db.execute("DELETE FROM vec_items WHERE rowid = ?", (row["rowid"],))
        db.execute(f"DELETE FROM vec_map WHERE memory_id IN ({marks})", tuple(memory_ids))
        db.execute(f"DELETE FROM edges WHERE memory_id IN ({marks})", tuple(memory_ids))
        db.execute(f"DELETE FROM typed_rows WHERE memory_id IN ({marks})", tuple(memory_ids))
        db.execute(f"DELETE FROM file_chunks WHERE memory_id IN ({marks})", tuple(memory_ids))
        counts["sources"] = db.execute(f"DELETE FROM memory_sources WHERE memory_id IN ({marks})", tuple(memory_ids)).rowcount
        db.execute(f"DELETE FROM lineage WHERE memory_id IN ({marks})", tuple(memory_ids))
        db.execute(f"DELETE FROM lineage WHERE child_kind = 'journal' AND child_id IN ({marks})", tuple(memory_ids))
        db.execute(f"DELETE FROM pool_members WHERE memory_id IN ({marks})", tuple(memory_ids))
        # Unlink supersede chains that pointed at a purged memory.
        db.execute(f"UPDATE memories SET superseded_by = NULL WHERE superseded_by IN ({marks})", tuple(memory_ids))
        counts["ledger"] = db.execute(f"DELETE FROM memories WHERE id IN ({marks})", tuple(memory_ids)).rowcount
        # Drop entities no edge references any more (their names are content too).
        db.execute("DELETE FROM entities WHERE name NOT IN (SELECT src FROM edges UNION SELECT dst FROM edges)")
    return counts
