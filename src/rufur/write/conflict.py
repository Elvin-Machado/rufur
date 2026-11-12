"""Update / contradiction engine (plan §6).

Candidates share the new claim's normalised (subject, predicate) key, or are
close vector neighbours in the same pool. Exact duplicates are decided by rule;
everything else goes to the classify-role LLM. Any doubt — including an LLM
that is down or returns junk — resolves to "unclear" for same-key pairs.
"""

from __future__ import annotations

import re
from typing import Literal

from pydantic import BaseModel

from ..db.conn import Database, pack_vector
from ..llm import LLM, LLMUnavailable
from ..textutil import norm_key

Outcome = Literal["duplicate", "update", "context", "unclear", "unrelated"]

LIVE_STATUSES = ("active", "disputed", "quarantined")

# An "update" must be backed by language that signals change in the source itself;
# otherwise two different values for one key are a contradiction, not an update.
CHANGE_MARKERS = re.compile(
    r"\b(now|moved|moving|chang(e|ed|ing)|switch(ed|ing)?|updat(e|ed|ing)|actually|no longer|not anymore|"
    r"instead|correct(ion|ed)?|postponed|extended|rescheduled|pushed (back|out)|delayed|brought forward|"
    r"replac(e|ed|ing)|from now on|these days|recently|nowadays|new)\b",
    re.I,
)


class Verdict(BaseModel):
    outcome: Outcome
    scope_old: str | None = None
    scope_new: str | None = None
    reason: str = ""


SYSTEM = """You compare an EXISTING memory with a NEW claim about the same thing and decide how they relate.

- duplicate: they say the same thing.
- update: the NEW claim replaces the EXISTING one (the fact changed over time, or the user corrected it).
  Choose this only when the new text clearly signals change or correction ("now", "moved", "changed to",
  "actually", "no longer", a newer date).
- context: both can be true at once because they apply to different situations (e.g. work vs. personal,
  different projects). Give a short scope for each.
- unclear: they conflict and you cannot tell which is right or whether it changed.
- unrelated: they are about different things.

When unsure between update and unclear, choose unclear."""


def find_candidates(db: Database, pool_ids: list[str], subject: str, predicate: str,
                    vec: list[float] | None, exclude_id: str | None = None) -> list[tuple[dict, str]]:  # fmt: skip
    """Returns [(memory, how)] where how is 'key' or 'vector'."""
    marks = ",".join("?" * len(pool_ids))
    status_marks = ",".join("?" * len(LIVE_STATUSES))
    found: dict[str, tuple[dict, str]] = {}
    if subject and predicate:
        for m in db.all(
            f"SELECT * FROM memories WHERE pool_id IN ({marks}) AND subject = ? AND predicate = ? AND status IN ({status_marks})",
            (*pool_ids, norm_key(subject), norm_key(predicate), *LIVE_STATUSES),
        ):
            found[m["id"]] = (m, "key")
    if vec is not None:
        rows = db.all(
            f"""WITH knn AS (SELECT rowid, distance FROM vec_items WHERE embedding MATCH ? AND k = 20)
                SELECT m.*, knn.distance FROM knn
                JOIN vec_map vm ON vm.rowid = knn.rowid
                JOIN memories m ON m.id = vm.memory_id
                WHERE vm.kind = 'memory' AND m.pool_id IN ({marks}) AND m.status IN ({status_marks})
                ORDER BY knn.distance""",
            (pack_vector(vec), *pool_ids, *LIVE_STATUSES),
        )
        for m in rows:
            # cosine distance; 0.15 ≈ similarity 0.85
            if m.pop("distance") <= 0.15 and m["id"] not in found:
                found[m["id"]] = (m, "vector")
            if len(found) >= 5:
                break
    found.pop(exclude_id or "", None)
    return list(found.values())


def classify(llm: LLM, existing: dict, new_text: str, how: str, new_value: str, evidence: str = "") -> tuple[Verdict, int]:
    """Returns (verdict, llm tokens used). `evidence` is the raw source text behind the new claim."""
    if how == "key" and norm_key(existing["value"]) == norm_key(new_value):
        return Verdict(outcome="duplicate", reason="same key and value"), 0
    prompt = f"EXISTING ({existing['source_type']}, trust {existing['trust']}): {existing['text']}\nNEW: {new_text}"
    try:
        verdict, usage = llm.complete("classify", SYSTEM, prompt, Verdict)
        tokens = usage.tokens_in + usage.tokens_out
    except LLMUnavailable:
        verdict, tokens = Verdict(outcome="unclear" if how == "key" else "unrelated", reason="classifier unavailable"), 0
    if verdict.outcome == "update" and not CHANGE_MARKERS.search(evidence or new_text):
        verdict = Verdict(outcome="unclear", reason=f"no change signal in the source; classifier said update: {verdict.reason}")
    if how == "key" and verdict.outcome == "unrelated":
        # Same subject and predicate can't be unrelated; treat the disagreement as unresolved.
        verdict = Verdict(outcome="unclear", reason=f"same key, classifier said unrelated: {verdict.reason}")
    return verdict, tokens
