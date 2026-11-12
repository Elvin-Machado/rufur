"""Importance at write time, and the decayed score used for automatic forgetting."""

from __future__ import annotations

import math

from ..textutil import now

TYPE_BASE = {
    "decision": 0.9,
    "preference": 0.75,
    "procedure": 0.7,
    "fact": 0.6,
    "relation": 0.55,
    "table": 0.55,
    "document": 0.5,
    "event": 0.4,
}


def initial_importance(mem_type: str, text: str) -> float:
    base = TYPE_BASE.get(mem_type, 0.5)
    # Very short claims carry less; cap the bonus so length can't dominate type.
    length_bonus = min(len(text) / 400, 0.1)
    return round(min(1.0, base + length_bonus), 3)


def retention_score(mem: dict, half_life_days: float) -> float:
    """Blend of base importance, recency of use and access count."""
    last = mem.get("last_accessed") or mem["created_at"]
    age_days = max(0.0, (now() - last) / 86400)
    recency = math.exp(-age_days * math.log(2) / half_life_days)
    access = min(1.0, math.log1p(mem.get("access_count") or 0) / math.log(20))
    return round(0.5 * mem["importance"] + 0.3 * recency + 0.2 * access, 4)
