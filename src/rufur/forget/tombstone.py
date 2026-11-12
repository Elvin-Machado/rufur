"""Salted-hash tombstones: remember *that* something was forgotten, never *what*."""

from __future__ import annotations

import secrets

from ..db.conn import Database
from ..textutil import norm_key, now, sha256


def _salt(db: Database) -> str:
    salt = db.meta("tombstone_salt")
    if salt is None:
        salt = secrets.token_hex(16)
        db.set_meta("tombstone_salt", salt)
    return salt


def hashes_for(db: Database, subject: str, predicate: str, value: str, text: str) -> list[tuple[str, str]]:
    salt = _salt(db)
    out = [("text", sha256(salt + norm_key(text)))]
    if subject and predicate and value:
        out.append(("key", sha256(salt + "|".join(norm_key(x) for x in (subject, predicate, value)))))
    return out


def add(db: Database, mem: dict) -> int:
    rows = hashes_for(db, mem["subject"], mem["predicate"], mem["value"], mem["text"])
    for kind, h in rows:
        db.execute("INSERT OR IGNORE INTO tombstones(hash, kind, created_at) VALUES(?,?,?)", (h, kind, now()))
    return len(rows)


def is_tombstoned(db: Database, subject: str, predicate: str, value: str, text: str) -> bool:
    hashes = [h for _, h in hashes_for(db, subject, predicate, value, text)]
    marks = ",".join("?" * len(hashes))
    return bool(db.scalar(f"SELECT 1 FROM tombstones WHERE hash IN ({marks}) LIMIT 1", tuple(hashes)))
