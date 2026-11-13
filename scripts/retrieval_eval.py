"""Retrieval and forget-candidate accuracy against a preset dataset with known answers.

    uv run python scripts/retrieval_eval.py [--hash] [--no-qwen] [--out docs/results/retrieval_eval.md]

Loads scripts/eval_data/retrieval.json into a throwaway RUFUR home, then:

1. Recall: every question through the fast path with no reranker, with MiniLM, and with Qwen3-Reranker.
   found = the expected memories were returned; precision = returned items that were expected;
   negatives = questions with no stored answer that correctly abstained.
2. Forget: every request through the old candidate search (key match + fast recall, everything it finds is
   forgotten on confirm) and the new one (Qwen scores candidates, the forget-role LLM decides).
   Only previews run, so every request sees the full dataset.

The write path uses a deterministic classifier (updates need a change word) so the stored state is the
same on every run; only the forget judge calls a real LLM. The verifier LLM is off so retrieval is
measured on its own.
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
import tempfile
import time
from pathlib import Path

from rufur.config import load_config
from rufur.embed import HashEmbedder, OllamaEmbedder
from rufur.llm import LLM, LLMResult, LLMUnavailable
from rufur.read.rerank import RerankUnavailable, make_reranker
from rufur.service import MemoryService
from rufur.write.conflict import CHANGE_MARKERS

DATA = Path(__file__).parent / "eval_data" / "retrieval.json"


class EvalLLM(LLM):
    """Real LLM for the forget judge only; everything else deterministic or unavailable."""

    def complete(self, role, system, user, schema):
        if role == "forget":
            return super().complete(role, system, user, schema)
        if role == "classify" and user.startswith("A: "):
            a, b = (line.split(" / ")[1].strip().lower() for line in user.splitlines()[:2])
            data = {"same": a == b}
        elif role == "classify":
            data = {"outcome": "update" if CHANGE_MARKERS.search(user.split("NEW:", 1)[1]) else "coexist",
                    "same_attribute": False, "reason": "eval"}  # fmt: skip
        else:
            raise LLMUnavailable(f"eval: {role} disabled")
        return schema.model_validate(data), LLMResult(data, "eval", 0, 0, 0.0)


def load(svc: MemoryService, data: dict) -> dict[str, str]:
    """Writes the facts in order; returns key → memory id."""
    ids = {}
    for f in data["facts"]:
        out = svc.remember("ui", f["text"], subject=f["subject"], predicate=f["predicate"], value=f["value"], source="user")
        ids[f["key"]] = out["results"][0]["memory"]["id"]
    return ids


def eval_recall(svc: MemoryService, data: dict, ids: dict[str, str]) -> dict:
    found, wanted, precision, latency, misses = 0, 0, [], [], []
    for q in data["questions"]:
        # Cutoff off: measures whether ranking surfaces the answer. Abstaining is measured on the negatives.
        res = svc.recall("ui", q["q"], threshold=0.0, record=False)
        got = {it["id"] for it in res["items"]}
        expect = {ids[k] for k in q["expect"]}
        found += len(got & expect)
        wanted += len(expect)
        precision.append(len(got & expect) / len(got) if got else 0.0)
        latency.append(res["latency_ms"])
        if missing := [k for k in q["expect"] if ids[k] not in got]:
            top = res["items"][0]["text"] if res["items"] else "(abstained)"
            misses.append(f"{q['q']} → missed {', '.join(missing)}; top: {top}")
    abstained = 0
    for q in data["negatives"]:
        res = svc.recall("ui", q, verify=False, record=False)
        latency.append(res["latency_ms"])
        if res["abstained"]:
            abstained += 1
        else:
            misses.append(f"{q} → should abstain; returned: {res['items'][0]['text']}")
    return {"found": found, "wanted": wanted, "precision": statistics.mean(precision),
            "abstained": abstained, "negatives": len(data["negatives"]),
            "p50": statistics.median(latency), "p95": sorted(latency)[int(0.95 * (len(latency) - 1))], "misses": misses}  # fmt: skip


def eval_forget(svc: MemoryService, data: dict, ids: dict[str, str]) -> dict:
    right, wanted, found, wrong, keeps, latency, notes = 0, 0, 0, 0, 0, [], []
    judge = None
    for r in data["forget"]:
        t = time.perf_counter()
        pre = svc.forget("ui", query=r["request"])
        latency.append((time.perf_counter() - t) * 1000)
        judge = pre["search"].get("judge")
        cands = {c["id"]: c for c in pre["candidates"]}
        # Without a judge, confirm forgets every candidate.
        gone = {cid for cid, c in cands.items() if c.get("decision", "forget") == "forget"}
        want = {ids[k] for k in r["forget"]}
        keep = {ids[k] for k in r["keep"]}
        found += len(want & set(cands))
        right += len(want & gone)
        wanted += len(want)
        wrong += len(keep & gone)
        keeps += len(keep)
        for k in r["forget"]:
            if ids[k] not in gone:
                notes.append(f"{r['request']} → would NOT forget {k}" + ("" if ids[k] in cands else " (never found)"))
        for k in r["keep"]:
            if ids[k] in gone:
                notes.append(f"{r['request']} → would WRONGLY forget {k}: {cands[ids[k]].get('reason', '')}")
        if n_other := len(gone - want - keep):
            notes.append(f"{r['request']} → would also forget {n_other} unlisted memories")
    return {"found": found, "right": right, "wanted": wanted, "wrong": wrong, "keeps": keeps, "judge": judge,
            "p50": statistics.median(latency), "notes": notes}  # fmt: skip


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--hash", action="store_true", help="hashing embedder instead of Ollama nomic-embed-text")
    ap.add_argument("--no-qwen", action="store_true", help="skip Qwen3-Reranker (MiniLM only)")
    ap.add_argument("--out", type=Path, help="also write the report as markdown here")
    args = ap.parse_args()
    data = json.loads(DATA.read_text())

    cfg = load_config(tempfile.mkdtemp(), fast_reranker="none", deep_reranker="none",
                      **({"embed_provider": "hash", "embed_dim": 256} if args.hash else {}))  # fmt: skip
    embedder = HashEmbedder(cfg.embed_dim) if args.hash else OllamaEmbedder(cfg)
    svc = MemoryService(cfg, llm=EvalLLM(cfg), embedder=embedder)
    ids = load(svc, data)
    print(f"loaded {len(ids)} memories ({embedder.name})", file=sys.stderr)

    rerankers = {"none": None, "minilm": make_reranker(cfg, "minilm")}
    if not args.no_qwen:
        rerankers["qwen"] = make_reranker(cfg, "qwen")
    for name, r in list(rerankers.items()):
        if r is None:
            continue
        try:
            r.score("warm up", ["load the model before timing"])
        except RerankUnavailable as e:
            print(f"skipping {name}: {e}", file=sys.stderr)
            rerankers.pop(name)

    recall_rows = {}
    for name, r in rerankers.items():
        svc.fast_reranker = r
        recall_rows[name] = eval_recall(svc, data, ids)
        print(f"recall with {name}: done", file=sys.stderr)
    svc.fast_reranker = None

    forget_rows = {}
    for name, r in (("old: key + recall", None), ("new: qwen + LLM judge", rerankers.get("qwen"))):
        if name.startswith("new") and r is None:
            continue
        svc.deep_reranker = r
        forget_rows[name] = eval_forget(svc, data, ids)
        print(f"forget with {name}: done", file=sys.stderr)
    svc.close()

    lines = [f"# Retrieval eval ({len(data['facts'])} memories, {embedder.name})", "",
             "## Recall (fast path)", "",
             "| reranker | found | precision | negatives abstained | p50 | p95 |", "|---|---|---|---|---|---|"]  # fmt: skip
    for name, r in recall_rows.items():
        lines.append(f"| {name} | {r['found']}/{r['wanted']} ({r['found'] / r['wanted']:.0%}) | {r['precision']:.0%} | "
                     f"{r['abstained']}/{r['negatives']} | {r['p50']:.0f} ms | {r['p95']:.0f} ms |")  # fmt: skip
    lines += ["", "found = expected memories returned (abstain cutoff off, so this is ranking quality). precision = share of returned items that were expected. negatives = questions with no stored answer that abstained at the default cutoff (verifier LLM off).", ""]
    for name, r in recall_rows.items():
        if r["misses"]:
            lines += [f"<details><summary>{name}: {len(r['misses'])} misses</summary>", ""]
            lines += [f"- {m}" for m in r["misses"]] + ["", "</details>", ""]
    lines += ["## Forget candidates", "",
              "| method | found | would forget (correct) | would forget (wrong) | judge | p50 |", "|---|---|---|---|---|---|"]  # fmt: skip
    for name, r in forget_rows.items():
        lines.append(f"| {name} | {r['found']}/{r['wanted']} | {r['right']}/{r['wanted']} | {r['wrong']}/{r['keeps']} | "
                     f"{r['judge'] or '-'} | {r['p50']:.0f} ms |")  # fmt: skip
    lines += ["", "found = memories the request covers that reached the candidate list. wrong = look-alike memories "
              "(listed as keep) that confirm would delete. Old method: confirm deletes every candidate.", ""]  # fmt: skip
    for name, r in forget_rows.items():
        if r["notes"]:
            lines += [f"<details><summary>{name}: {len(r['notes'])} notes</summary>", ""]
            lines += [f"- {n}" for n in r["notes"]] + ["", "</details>", ""]
    report = "\n".join(lines)
    print(report)
    if args.out:
        args.out.write_text(report + "\n")


if __name__ == "__main__":
    main()
