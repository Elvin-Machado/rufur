"""Small text helpers shared by the write, read and forget paths."""

from __future__ import annotations

import hashlib
import re
import time
import uuid

_WS = re.compile(r"\s+")
_WORD = re.compile(r"[A-Za-z0-9][A-Za-z0-9'_-]*")

STOPWORDS = frozenset(
    """a an and are as at be been but by can could did do does for from had has have how i if in is it its
    me my of on or our should so than that the their them then there these they this to was we were what
    when where which who whom why will with would you your about any some tell know remember please""".split()
)


# Seconds added to the wall clock. Only tests move it, to exercise expiry and revalidation.
CLOCK_OFFSET = 0.0


def now() -> float:
    return time.time() + CLOCK_OFFSET


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


def norm_ws(text: str) -> str:
    return _WS.sub(" ", text).strip()


def norm_key(text: str) -> str:
    """Canonical form for subject/predicate matching: lowercase, single spaces, no punctuation."""
    return norm_ws(re.sub(r"[^\w\s]", " ", text.lower()))


def content_words(text: str) -> list[str]:
    return [w for w in (m.group(0).lower() for m in _WORD.finditer(text)) if w not in STOPWORDS and len(w) > 1]


def estimate_tokens(text: str) -> int:
    # ~4 chars per token is close enough for budgeting and for the baseline comparison.
    return max(1, (len(text) + 3) // 4)


def sha256(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def quote_in(quote: str, source: str) -> bool:
    """True if `quote` appears verbatim in `source`, ignoring case and whitespace runs."""
    q = norm_ws(quote).lower()
    return bool(q) and q in norm_ws(source).lower()
