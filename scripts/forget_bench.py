"""Deletion requests end to end, at growing memory sizes.

    uv run python scripts/forget_bench.py [--sizes 1000 5000] [--requests 20] [--real]

Each request is what a user would say ("forget contact 17's phone number"). The script follows
the agent flow: forget by query → pick the candidates the user means → forget them → forget the
other memories the response says still mention the value. Then a *different* agent tries to get
the value back through recall, recall_past and a handover resume. A request counts as completed
only if none of them return it and the receipt chain still verifies.

Also reports what erasure costs now (secure-delete + WAL truncate) against the old path that
VACUUMed the whole database on every forget.
"""

from __future__ import annotations

import argparse
import statistics
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tests"))

from rufur.config import load_config
from rufur.embed import HashEmbedder, OllamaEmbedder
from rufur.handover import HandoverIn
from rufur.llm import LLM, FakeLLM
from rufur.service import MemoryService

ASKER = "other-agent"


def phone(i: int) -> str:
    return f"555-{7000 + i:04d}"


def build(svc: MemoryService, n: int) -> None:
    """n memories: contacts with a phone number (each later updated once), plus filler facts."""
    contacts = max(40, n // 10)
    for i in range(contacts):
        kw = dict(subject=f"contact {i}", predicate="phone number", source="user")
        svc.remember("ui", f"Contact {i}'s phone number is {phone(i)}.", value=phone(i), **kw)
    for i in range(contacts):  # history: every number changed once, so a forget must take both versions
        kw = dict(subject=f"contact {i}", predicate="phone number", source="user")
        svc.remember("ui", f"Contact {i} changed their phone number to {phone(i + 5000)}.", value=phone(i + 5000), **kw)
    for i in range(min(contacts, 40)):  # a different fact that mentions the number
        svc.remember("ui", f"Call contact {i} on {phone(i + 5000)} about the Q3 invoice.", subject=f"contact {i}",
                     predicate="todo", value=f"call about Q3 invoice", source="user")  # fmt: skip
    for i in range(n - 2 * contacts - min(contacts, 40)):
        svc.remember("ui", f"Project note {i}: module {i % 97} owns feature {i}.", subject=f"module {i % 97}",
                     predicate=f"feature {i}", value=f"feature {i}", source="user")  # fmt: skip


def leaked(svc: MemoryService, i: int, task: str) -> list[str]:
    values = (phone(i), phone(i + 5000))
    where = []
    q = f"What is contact {i}'s phone number?"
    for name, res in (("recall", svc.recall(ASKER, q)), ("recall_past", svc.recall_past(ASKER, q))):
        if any(v in res.get("context", "") for v in values):
            where.append(name)
    resumed = svc.handover_resume(ASKER, task)
    blob = str(resumed["packet"]) + resumed["context"].get("context", "")
    if any(v in blob for v in values):
        where.append("handover")
    return where


def run(size: int, requests: int, real: bool) -> dict:
    cfg = load_config(tempfile.mkdtemp(), **({} if real else {"embed_provider": "hash", "embed_dim": 256}))
    if real:
        svc = MemoryService(cfg, llm=LLM(cfg), embedder=OllamaEmbedder(cfg))
    else:
        from conftest import fake_handler

        svc = MemoryService(cfg, llm=FakeLLM(cfg, fake_handler), embedder=HashEmbedder(cfg.embed_dim))
    t = time.perf_counter()
    build(svc, size)
    build_s = time.perf_counter() - t
    completed, erase, total, completeness, informative, escalated = 0, [], [], [], [], 0
    failures = []
    for i in range(requests):
        task = f"call-contact-{i}"
        svc.handover_create("ui", task, HandoverIn(goal=f"Call contact {i}", steps_pending=[f"dial {phone(i + 5000)}"]))
        pre = svc.forget("ui", query=f"contact {i} phone number")
        # The agent shows the candidates; the user means this contact's phone number, and agrees that the
        # other memories listed under also_mentioned_in should go too.
        chosen = [c["id"] for c in pre["candidates"]
                  if (m := svc.ledger.get_memory(c["id"]))["subject"] == f"contact {i}" and m["predicate"] == "phone number"]  # fmt: skip
        out = svc.forget("ui", memory_ids=chosen[:1], cascade_mentions=True) if chosen else {"forgotten": [], "report": None}
        if out.get("report"):
            erase.append(out["timing_ms"]["erase"])
            total.append(out["timing_ms"]["total"])
            completeness.append(out["report"]["completeness"])
            informative.append(out["report"]["probes_informative"] / out["report"]["probes_total"])
            escalated += bool(out["report"]["escalated"])
        where = leaked(svc, i, task)
        if out.get("forgotten") and not where:
            completed += 1
        else:
            failures.append((i, where or "nothing selected"))
    chain = svc.verify_forgetting("ui")
    t = time.perf_counter()
    svc.db.compact()  # what every forget used to cost
    vacuum_ms = (time.perf_counter() - t) * 1000
    db_mb = cfg.db_path.stat().st_size / 1e6
    svc.close()
    return {"size": size, "db_mb": db_mb, "build_s": build_s, "completed": completed, "requests": requests,
            "completeness": statistics.mean(completeness or [0]), "informative": statistics.mean(informative or [0]),
            "erase_p50": statistics.median(erase or [0]), "total_p50": statistics.median(total or [0]),
            "vacuum_ms": vacuum_ms, "escalated": escalated, "chain_valid": chain["valid"], "failures": failures}  # fmt: skip


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--sizes", type=int, nargs="+", default=[1000, 5000])
    ap.add_argument("--requests", type=int, default=20)
    ap.add_argument("--real", action="store_true", help="Ollama embeddings and LLM probe paraphrases (slow)")
    args = ap.parse_args()
    rows = []
    for n in args.sizes:
        r = run(n, args.requests, args.real)
        rows.append(r)
        print(f"  built {n} memories in {r['build_s']:.0f}s", file=sys.stderr)
        for f in r["failures"]:
            print(f"  FAILED request {f[0]}: {f[1]}", file=sys.stderr)
    print(f"{'memories':>9}{'db MB':>7}{'completed':>11}{'complete':>9}{'informative':>12}"
          f"{'erase p50':>10}{'forget p50':>11}{'old VACUUM':>11}{'escalated':>10}{'receipts':>9}")
    for r in rows:
        print(f"{r['size']:>9}{r['db_mb']:>7.1f}{r['completed']:>6}/{r['requests']:<4}{r['completeness']:>9.2f}"
              f"{r['informative']:>12.0%}{r['erase_p50']:>8.1f}ms{r['total_p50']:>9.1f}ms{r['vacuum_ms']:>9.1f}ms"
              f"{r['escalated']:>6}/{r['requests']:<3}{'valid' if r['chain_valid'] else 'BROKEN':>9}")
    print("\ncompleted = a different agent can't get the number back via recall, recall_past or a handover.")
    print("complete = share of informative probes (each saw the fact before deletion) that find nothing after.")
    print("old VACUUM = the full-database rewrite every forget used to do. Now it only runs when the byte scan")
    print("  finds a stale copy left on disk (escalated), so the guarantee holds and the common case stays cheap.")


if __name__ == "__main__":
    main()
