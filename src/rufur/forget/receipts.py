"""Deletion receipts: signed, hash-chained proof that a forget happened and what its probes found.

A receipt holds ids, salted tombstone hashes, counts and probe outcomes, never the forgotten
content. Each receipt's hash covers the previous one's, and an HMAC with a key kept outside the
database signs it, so deleting, editing or reordering receipts breaks `verify_chain`.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets
from pathlib import Path

from ..config import Config
from ..db.conn import Database
from ..textutil import new_id, now
from . import tombstone

GENESIS = "0" * 64


def _key(cfg: Config) -> bytes:
    path = Path(cfg.home) / "receipt.key"
    if not path.exists():
        path.write_text(secrets.token_hex(32))
        os.chmod(path, 0o600)
    return bytes.fromhex(path.read_text().strip())


def _canonical(body: dict) -> str:
    return json.dumps(body, sort_keys=True, separators=(",", ":"))


def file_sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def issue(cfg: Config, db: Database, agent: str, mems: list[dict], purged_ids: list[str], counts: dict,
          report: dict | None) -> dict:  # fmt: skip
    prev = db.one("SELECT hash FROM forget_receipts ORDER BY seq DESC LIMIT 1")
    prev_hash = prev["hash"] if prev else GENESIS
    body = {
        "receipt_id": new_id("rcpt"),
        "agent": agent,
        "issued_at": now(),
        "memory_ids": purged_ids,
        "target_hashes": sorted(h for m in mems for _, h in tombstone.hashes_for(db, m["subject"], m["predicate"],
                                                                                   m["value"], m["text"])),  # fmt: skip
        "counts": counts,
        "probes": report["probe_summaries"] if report else [],
        "completeness": report["completeness"] if report else None,
        "erasure": "sqlite secure_delete + fts5 secure-delete + wal truncate",
        "db_sha256_after": file_sha256(cfg.db_path),
        "prev_hash": prev_hash,
    }
    digest = hashlib.sha256((prev_hash + _canonical(body)).encode()).hexdigest()
    sig = hmac.new(_key(cfg), digest.encode(), hashlib.sha256).hexdigest()
    db.execute("INSERT INTO forget_receipts(id, prev_hash, hash, sig, body, created_at) VALUES(?,?,?,?,?,?)",
               (body["receipt_id"], prev_hash, digest, sig, _canonical(body), body["issued_at"]))  # fmt: skip
    return {**body, "hash": digest, "sig": sig}


def list_receipts(db: Database, limit: int = 50) -> list[dict]:
    rows = db.all("SELECT * FROM forget_receipts ORDER BY seq DESC LIMIT ?", (limit,))
    return [{**json.loads(r["body"]), "hash": r["hash"], "sig": r["sig"]} for r in rows]


def get(db: Database, receipt_id: str) -> dict | None:
    r = db.one("SELECT * FROM forget_receipts WHERE id = ?", (receipt_id,))
    return {**json.loads(r["body"]), "hash": r["hash"], "sig": r["sig"]} if r else None


def recheck(db: Database) -> list[str]:
    """Live memories whose text or key hashes to a tombstone: forgotten content that came back by a path
    that skipped the write-time check (a sync import, a merge, a hand edit)."""
    back = []
    for m in db.all("SELECT id, subject, predicate, value, text FROM memories"):
        if tombstone.is_tombstoned(db, m["subject"], m["predicate"], m["value"], m["text"]):
            back.append(m["id"])
    return back


def verify_chain(cfg: Config, db: Database) -> dict:
    """Recompute every hash and signature, and re-check that each receipt's memories are still absent."""
    key = _key(cfg)
    prev_hash = GENESIS
    problems: list[dict] = []
    rows = db.all("SELECT * FROM forget_receipts ORDER BY seq")
    for r in rows:
        body = json.loads(r["body"])
        digest = hashlib.sha256((r["prev_hash"] + _canonical(body)).encode()).hexdigest()
        if r["prev_hash"] != prev_hash or body.get("prev_hash") != prev_hash:
            problems.append({"receipt": r["id"], "problem": "chain broken (a receipt was removed or reordered)"})
        if digest != r["hash"]:
            problems.append({"receipt": r["id"], "problem": "body altered after signing"})
        if not hmac.compare_digest(hmac.new(key, r["hash"].encode(), hashlib.sha256).hexdigest(), r["sig"]):
            problems.append({"receipt": r["id"], "problem": "bad signature"})
        ids = body.get("memory_ids", [])
        if ids:
            marks = ",".join("?" * len(ids))
            if db.scalar(f"SELECT count(*) FROM memories WHERE id IN ({marks})", tuple(ids)):
                problems.append({"receipt": r["id"], "problem": "a forgotten memory id exists again"})
        prev_hash = r["hash"]
    resurfaced = recheck(db)
    return {"receipts": len(rows), "valid": not problems and not resurfaced, "problems": problems,
            "resurfaced_memories": resurfaced}  # fmt: skip
