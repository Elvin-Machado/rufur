"""Deterministic trust and quarantine rules (plan §5.4, §8)."""

from __future__ import annotations

import re

TRUST_ORDER = ["low", "medium_low", "medium", "high"]
TRUST_WEIGHT = {"low": 0.25, "medium_low": 0.6, "medium": 0.8, "high": 1.0}
SENSITIVITY_ORDER = ["public", "internal", "confidential", "restricted"]

SOURCE_TRUST = {
    "user": "high",
    "document": "medium",
    "agent": "medium_low",
    "tool": "low",
    "web": "low",
}

ROLE_SOURCE = {"user": "user", "assistant": "agent", "tool": "tool", "document": "document", "web": "web"}

# Text that tries to steer the model rather than state a fact.
_INSTRUCTION_LIKE = re.compile(
    r"\b(ignore (all |any )?(previous|prior|above)|disregard (the|all|previous)|you must|you are now|"
    r"system prompt|always (answer|respond|say|reply)|from now on|do not tell|new instructions|"
    r"override|act as|pretend (to be|you))\b",
    re.I,
)


def trust_for(source_type: str) -> str:
    return SOURCE_TRUST.get(source_type, "low")


def min_trust(a: str, b: str) -> str:
    return a if TRUST_ORDER.index(a) <= TRUST_ORDER.index(b) else b


def trust_at_least(trust: str, floor: str) -> bool:
    return TRUST_ORDER.index(trust) >= TRUST_ORDER.index(floor)


def instruction_like(text: str) -> bool:
    return bool(_INSTRUCTION_LIKE.search(text))


def can_read(clearance: str, sensitivity: str) -> bool:
    return SENSITIVITY_ORDER.index(sensitivity) <= SENSITIVITY_ORDER.index(clearance)


def initial_status(trust: str, flags: list[str]) -> str:
    """Low-trust and instruction-like memories start quarantined."""
    if trust == "low" or "instruction_like" in flags:
        return "quarantined"
    return "active"
