"""Which storage fits which kind of knowledge? Same corpus, five storage strategies, measured.

    uv run python scripts/storage_choice.py [--hash] [--k 5]

Kinds: profile, atomic facts, tables, relations (multi-hop), procedures, history (changing values).
Strategies, matching the options the problem statement lists:
  prompt  everything of that kind pasted into the prompt
  vector  top-k by embedding (nomic-embed-text; --hash for the offline embedder)
  fts     top-k by BM25 keyword search
  files   one file per entity; the agent opens the files whose name the question mentions (tool calls)
  code    a query the agent writes against structured storage (SQL over rows, graph walk, key lookup,
          validity-window lookup)

No LLM judge. Each question names the exact units of evidence it needs; a strategy is correct only
if its context contains all of them (for `code`, if the query returns the right answer). Tokens are
what the strategy puts into the prompt. Writes docs/results/storage_choice.md.
"""

from __future__ import annotations

import argparse
import random
import re
import sqlite3
import statistics
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from rufur.config import load_config
from rufur.embed import HashEmbedder, OllamaEmbedder
from rufur.textutil import estimate_tokens

ROOT = Path(__file__).resolve().parents[1]
rng = random.Random(7)


@dataclass
class Unit:
    id: str
    text: str
    file: str  # entity the file-per-entity layout groups it under
    data: dict = field(default_factory=dict)  # structured form, for the `code` strategy


@dataclass
class Question:
    text: str
    needs: set[str]  # unit ids that must all be in context
    answer: str  # what the `code` strategy must return
    code: Callable[["Kind"], str]


@dataclass
class Kind:
    name: str
    units: list[Unit]
    questions: list[Question]
    # What the agent must read and write to use `code`: the schema plus a typical query. None = not applicable.
    code_cost: str | None = None


# ---------------------------------------------------------------- corpus

NAMES = ["Asha", "Ravi", "Meera", "Karan", "Divya", "Arjun", "Nina", "Omar", "Priya", "Leo", "Sara", "Vikram",
         "Tara", "Imran", "Zoe", "Kabir", "Lena", "Rohan", "Isha", "Dev", "Maya", "Neil", "Anya", "Farah"]  # fmt: skip


def profile() -> Kind:
    facts = [("home city", "Bangalore"), ("preferred editor", "Neovim"), ("indentation", "tabs"),
             ("answer style", "short answers"), ("theme", "dark mode"), ("dog", "Bruno"),
             ("time zone", "IST"), ("language", "Go")]  # fmt: skip
    units = [Unit(f"p{i}", f"The user's {k} is {v}.", "user", {"key": k, "value": v}) for i, (k, v) in enumerate(facts)]
    qs = [Question(f"Write me a snippet; follow my usual {k} preference.", {u.id}, u.data["value"],
                   lambda K, k=k: next(u.data["value"] for u in K.units if u.data["key"] == k))
          for u, (k, _) in zip(units, facts) if k in ("indentation", "answer style", "theme", "language")]  # fmt: skip
    # The profile matters to every request, usually without being asked about directly.
    qs.append(Question("Help me set up a new project the way I like it.", {u.id for u in units}, "all",
                       lambda K: "all"))  # fmt: skip
    return Kind("profile", units, qs)  # code n/a: nobody queries "my preferences" explicitly


def facts() -> Kind:
    units, qs = [], []
    for i in range(300):
        port, team = 7000 + rng.randrange(2000), NAMES[i % len(NAMES)]
        u = Unit(f"f{i}", f"Service svc-{i} listens on port {port} and is owned by {team}'s team.", f"svc-{i}",
                 {"service": f"svc-{i}", "port": str(port), "owner": team})  # fmt: skip
        units.append(u)
    for i in rng.sample(range(300), 15):
        u = units[i]
        qs.append(Question(f"Which port does svc-{i} listen on?", {u.id}, u.data["port"],
                           lambda K, s=f"svc-{i}": next(x.data["port"] for x in K.units if x.data["service"] == s)))  # fmt: skip
    return Kind("atomic facts", units, qs, "facts(service TEXT, attribute TEXT, value TEXT)\n"
                "SELECT value FROM facts WHERE service = 'svc-17' AND attribute = 'port'")


def tables() -> Kind:
    clients = ["Acme", "Globex", "Initech", "Umbrella", "Hooli", "Stark"]
    months = ["January", "February", "March", "April"]
    units = []
    for i in range(200):
        c, m, amt = rng.choice(clients), rng.choice(months), rng.randrange(100, 3000)
        units.append(Unit(f"t{i}", f"Invoice INV-{1000 + i} for {c} in {m} was {amt} USD.", c,
                          {"client": c, "month": m, "amount": amt}))  # fmt: skip
    qs = []
    for c, m in [("Acme", "March"), ("Globex", "January"), ("Hooli", "April"), ("Stark", "February"), ("Initech", "March")]:
        rows = [u for u in units if u.data["client"] == c and u.data["month"] == m]
        qs.append(Question(f"What was the total invoiced to {c} in {m}?", {u.id for u in rows},
                           str(sum(u.data["amount"] for u in rows)),
                           lambda K, c=c, m=m: str(K.db.execute(
                               "SELECT sum(amount) FROM inv WHERE client = ? AND month = ?", (c, m)).fetchone()[0])))  # fmt: skip
    for c in ["Umbrella", "Acme"]:
        rows = [u for u in units if u.data["client"] == c and u.data["amount"] > 2000]
        qs.append(Question(f"How many {c} invoices were over 2000 USD?", {u.id for u in rows}, str(len(rows)),
                           lambda K, c=c: str(K.db.execute(
                               "SELECT count(*) FROM inv WHERE client = ? AND amount > 2000", (c,)).fetchone()[0])))  # fmt: skip
    for i in rng.sample(range(200), 3):
        u = units[i]
        qs.append(Question(f"How much was invoice INV-{1000 + i}?", {u.id}, str(u.data["amount"]),
                           lambda K, n=1000 + i: str(next(x.data["amount"] for x in K.units if f"INV-{n} " in x.text))))  # fmt: skip
    return Kind("tables", units, qs, "invoices(number TEXT, client TEXT, month TEXT, amount INTEGER)\n"
                "SELECT sum(amount) FROM invoices WHERE client = 'Acme' AND month = 'March'")


def relations() -> Kind:
    people = [f"{n} {s}" for s in ("Rao", "Iyer", "Shah") for n in NAMES][:60]
    boss = {p: (people[(i - 1) // 3] if i else None) for i, p in enumerate(people)}  # a 3-ary org tree
    units = [Unit(f"r{i}", f"{p} reports to {boss[p]}.", p, {"src": p, "dst": boss[p]})
             for i, p in enumerate(people) if boss[p]]  # fmt: skip
    by_src = {u.data["src"]: u for u in units}
    qs = []
    for p in rng.sample([p for p in people if boss[p] and boss[boss[p]]], 10):
        mid = boss[p]
        qs.append(Question(f"Who is {p}'s skip-level manager (their manager's manager)?", {by_src[p].id, by_src[mid].id},
                           boss[mid], lambda K, p=p: K.walk(p, 2)))  # fmt: skip
    return Kind("relations (2-hop)", units, qs, "edges(src TEXT, relation TEXT, dst TEXT)\n"
                "SELECT b.dst FROM edges a JOIN edges b ON b.src = a.dst WHERE a.src = 'Asha Rao' "
                "AND a.relation = 'reports to' AND b.relation = 'reports to'")


def procedures() -> Kind:
    topics = ["deploy", "rollback", "rotate keys", "restore backup", "failover", "onboard a teammate"]
    verbs = ["check", "run", "open", "confirm", "update", "notify", "verify", "tag", "drain", "record"]
    units, qs = [], []
    for t in topics:
        steps = [f"Step {n} to {t}: {rng.choice(verbs)} the {rng.choice(['runbook', 'dashboard', 'queue', 'registry', 'cluster', 'pager', 'ledger'])} "
                 f"({rng.choice(['us-east', 'eu-west', 'ap-south'])}, ticket OPS-{rng.randrange(100, 999)})." for n in range(1, 13)]  # fmt: skip
        ids = []
        for n, s in enumerate(steps, 1):
            units.append(Unit(f"{t}-{n}", s, t.replace(" ", "-"), {"topic": t, "n": n}))
            ids.append(f"{t}-{n}")
        qs.append(Question(f"Give me every step, in order, to {t}.", set(ids), "12 steps",
                           lambda K, t=t: f"{len([u for u in K.units if u.data['topic'] == t])} steps"))  # fmt: skip
        qs.append(Question(f"What is step 7 when I {t}?", {f"{t}-7"}, units[-6].text,
                           lambda K, t=t: next(u.text for u in K.units if u.data["topic"] == t and u.data["n"] == 7)))  # fmt: skip
    return Kind("procedures", units, qs)  # code n/a: the answer is the text of the steps


def history() -> Kind:
    units, qs = [], []
    for i in range(30):
        team = f"team-{i}"
        people = rng.sample(NAMES, 3)
        for v, p in enumerate(people):
            t0, t1 = 1_700_000_000 + v * 86400 * 30, (1_700_000_000 + (v + 1) * 86400 * 30 if v < 2 else None)
            units.append(Unit(f"h{i}-{v}", f"{p} is the on-call engineer for {team}.", team,
                              {"team": team, "person": p, "valid_from": t0, "valid_to": t1}))  # fmt: skip
        cur, prev = units[-1], units[-2]
        if i < 8:
            # Correct means the current holder is in context *and* no stale holder is presented as current.
            qs.append(Question(f"Who is on call for {team} right now?", {cur.id}, cur.data["person"],
                               lambda K, team=team: K.current(team)))  # fmt: skip
            qs[-1].forbid = {u.id for u in units[-3:-1]}
        elif i < 12:
            qs.append(Question(f"Who was on call for {team} before {cur.data['person']}?", {prev.id}, prev.data["person"],
                               lambda K, team=team: K.before_current(team)))  # fmt: skip
    return Kind("history (changing values)", units, qs, "facts(subject TEXT, attribute TEXT, value TEXT, valid_from REAL, valid_to REAL)\n"
                "SELECT value FROM facts WHERE subject = 'team-3' AND attribute = 'on-call' AND valid_to IS NULL")


# ------------------------------------------------------------- strategies


class Store:
    def __init__(self, kind: Kind, embedder):
        self.kind = kind
        self.units = kind.units
        self.by_id = {u.id: u for u in kind.units}
        self.vecs = embedder.embed([u.text for u in kind.units])
        self.embedder = embedder
        self.fts = sqlite3.connect(":memory:")
        self.fts.execute("CREATE VIRTUAL TABLE f USING fts5(text, uid UNINDEXED, tokenize='porter unicode61')")
        self.fts.executemany("INSERT INTO f VALUES(?, ?)", [(u.text, u.id) for u in kind.units])
        self.files: dict[str, list[Unit]] = {}
        for u in kind.units:
            self.files.setdefault(u.file, []).append(u)
        # Structured side: what `code` queries.
        kind.db = sqlite3.connect(":memory:")
        if kind.name == "tables":
            kind.db.execute("CREATE TABLE inv(client, month, amount)")
            kind.db.executemany("INSERT INTO inv VALUES(?,?,?)", [(u.data["client"], u.data["month"], u.data["amount"])
                                                                   for u in kind.units])  # fmt: skip
        edges = {u.data["src"]: u.data["dst"] for u in kind.units if "src" in u.data}

        def walk(p: str, hops: int) -> str:
            for _ in range(hops):
                p = edges[p]
            return p

        def current(team: str) -> str:
            return next(u.data["person"] for u in kind.units if u.data.get("team") == team and u.data["valid_to"] is None)

        def before_current(team: str) -> str:
            rows = sorted((u for u in kind.units if u.data.get("team") == team), key=lambda u: u.data["valid_from"])
            return rows[-2].data["person"]

        kind.walk, kind.current, kind.before_current = walk, current, before_current

    def prompt(self, q: str) -> list[Unit]:
        return self.units

    def vector(self, q: str, k: int) -> list[Unit]:
        qv = self.embedder.embed([q], kind="query")[0]
        scored = sorted(range(len(self.units)), key=lambda i: -sum(a * b for a, b in zip(qv, self.vecs[i])))
        return [self.units[i] for i in scored[:k]]

    def fts_search(self, q: str, k: int) -> list[Unit]:
        words = [w for w in re.findall(r"[A-Za-z0-9-]+", q) if len(w) > 2]
        expr = " OR ".join(f'"{w}"' for w in words)
        rows = self.fts.execute("SELECT uid FROM f WHERE f MATCH ? ORDER BY bm25(f) LIMIT ?", (expr, k)).fetchall()
        return [self.by_id[r[0]] for r in rows]

    def file_read(self, q: str) -> list[Unit]:
        low = q.lower()
        names = [f for f in self.files if f.lower() in low or f.lower().replace("-", " ") in low]
        if not names:
            names = ["user"] if "user" in self.files else []
        # Longest match first: "svc-17" must not also open "svc-1".
        names = sorted(names, key=len, reverse=True)[:1] if names and self.kind.name == "atomic facts" else names
        return [u for f in names for u in self.files[f]]


def evaluate(kind: Kind, store: Store, k: int) -> dict[str, dict]:
    strategies = {
        "prompt": lambda q: store.prompt(q.text),
        "vector": lambda q: store.vector(q.text, k),
        "fts": lambda q: store.fts_search(q.text, k),
        "files": lambda q: store.file_read(q.text),
    }
    results: dict[str, dict] = {}
    for name, fn in strategies.items():
        ok, toks, lat = 0, [], []
        for q in kind.questions:
            t = time.perf_counter()
            ctx = fn(q)
            lat.append((time.perf_counter() - t) * 1000)
            ids = {u.id for u in ctx}
            good = q.needs <= ids
            forbid = getattr(q, "forbid", set())
            if forbid and forbid & ids:
                good = False  # stale versions in context with nothing saying which is current
            ok += good
            toks.append(estimate_tokens("\n".join(u.text for u in ctx)))
        results[name] = {"acc": ok / len(kind.questions), "tokens": statistics.mean(toks), "ms": statistics.median(lat)}
    if kind.code_cost is None:
        results["code"] = None
        return results
    ok, toks, lat = 0, [], []
    for q in kind.questions:
        t = time.perf_counter()
        ans = q.code(kind)
        lat.append((time.perf_counter() - t) * 1000)
        ok += ans == q.answer
        toks.append(estimate_tokens(kind.code_cost) + estimate_tokens(ans))
    results["code"] = {"acc": ok / len(kind.questions), "tokens": statistics.mean(toks), "ms": statistics.median(lat)}
    return results


RUFUR_PLACEMENT = {
    "profile": "profile → working set injected at session start (prompt)",
    "atomic facts": "fact store → hybrid FTS + vector recall",
    "tables": "typed_rows table (code/SQL), plus a fact sentence for recall",
    "relations (2-hop)": "graph edges (code walk), plus a fact sentence for recall",
    "procedures": "markdown file + chunks (files), retrieved whole",
    "history (changing values)": "ledger with validity windows; recall returns current, recall_past the old",
}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--hash", action="store_true", help="offline hashing embedder instead of nomic-embed-text")
    ap.add_argument("--k", type=int, default=5)
    args = ap.parse_args()
    cfg = load_config(tempfile.mkdtemp(), **({"embed_provider": "hash", "embed_dim": 256} if args.hash else {}))
    embedder = HashEmbedder(cfg.embed_dim) if args.hash else OllamaEmbedder(cfg)
    kinds = [profile(), facts(), tables(), relations(), procedures(), history()]
    names = ["prompt", "vector", "fts", "files", "code"]
    lines = [f"# Which storage fits which knowledge\n",
             f"Embedder: {'hash (offline)' if args.hash else 'nomic-embed-text'}; top-k = {args.k}. "
             "Cell = share of questions whose context held *all* the needed evidence (code: right answer) · "
             "average prompt tokens.\n",
             "| knowledge | units | " + " | ".join(names) + " | best | what RUFUR does |",
             "|---|---|" + "---|" * len(names) + "---|---|"]  # fmt: skip
    for kind in kinds:
        res = evaluate(kind, Store(kind, embedder), args.k)
        # Best = highest accuracy, then fewest tokens.
        best = max((n for n in names if res[n]), key=lambda n: (round(res[n]["acc"], 3), -res[n]["tokens"]))
        cells = [f"{res[n]['acc']:.0%} · {res[n]['tokens']:.0f}" if res[n] else "n/a" for n in names]
        lines.append(f"| {kind.name} | {len(kind.units)} | " + " | ".join(cells) + f" | **{best}** | {RUFUR_PLACEMENT[kind.name]} |")
        print(f"{kind.name:<28}" + "".join(f"{n}={res[n]['acc']:.0%}/{res[n]['tokens']:.0f}t  " if res[n] else f"{n}=n/a  " for n in names) + f"best={best}")
    lines += ["",
              "Reading the table:",
              "- **prompt** is always complete but its cost grows with everything you know; it only wins for the small profile that every request needs.",
              "- **vector / fts** are cheap for single facts but miss evidence spread over many units (totals, all steps of a procedure, both hops of a relation) and return stale versions next to current ones.",
              "- **files** win when knowledge is naturally grouped (a whole procedure) and the question names the group.",
              "- **code** (SQL, graph walk, validity lookup) is exact and nearly free in tokens for aggregates, multi-hop and \"what is true now\", but needs structured storage written at capture time, and its token cost includes the schema and query the agent has to write (an LLM writing the wrong query is a failure mode this table does not measure).",
              ""]  # fmt: skip
    out = ROOT / "docs" / "results" / "storage_choice.md"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text("\n".join(lines))
    print(f"report: {out}")


if __name__ == "__main__":
    main()
