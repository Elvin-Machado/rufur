"""MemoryService: the only place with business logic.

REST and MCP are thin adapters over this class, so both APIs always expose
exactly the same behaviour. Every public method takes the calling agent's name
first; access control is applied here, before any search or write.
"""

from __future__ import annotations

import json
import secrets
import time
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor
from typing import Any

from .config import Config
from .db.conn import Database, dumps
from .embed import EmbedUnavailable, make_embedder
from .events_bus import EventBus
from .forget import engine as forget_engine
from .forget.purge import purge_memories
from . import handover as ho
from .ledger import Ledger
from .llm import LLM, LLMUnavailable
from .read.recall import CONTEXT_HEADER, fast_recall, render
from .read.search import Filter
from .read.verify import make_verifier
from .read import working_set as ws_mod
from . import pools as pools_mod, stm as stm_mod
from .write import consolidate as consolidate_mod
from .textutil import estimate_tokens, new_id, now, quote_in, sha256
from .write.extract import extract_claims
from .write.importance import retention_score
from .write.pipeline import ClaimIn, WritePath
from .write.trust import ROLE_SOURCE, SENSITIVITY_ORDER, TRUST_ORDER


class AccessDenied(PermissionError):
    pass


class NotFound(LookupError):
    pass


def _acl_allows(acl: dict, perm: str, agent: str, user_id: str | None = None) -> bool:
    """ACL entries are "*", an agent name, or "user:<id>" (every agent of that user).

    Subagent identities (user "scratch:<pool>") never match "*": they may see only what their pool grants.
    """
    allowed = acl.get(perm, [])
    scratch = user_id is not None and user_id.startswith("scratch:")
    return (("*" in allowed and not scratch) or agent in allowed
            or (user_id is not None and f"user:{user_id}" in allowed))  # fmt: skip


class MemoryService:
    def __init__(self, cfg: Config, llm: LLM | None = None, embedder=None):
        cfg.ensure_dirs()
        self.cfg = cfg
        self.bus = EventBus()
        self.embedder = embedder or make_embedder(cfg)
        self.db = Database(cfg.db_path, cfg.embed_dim)
        self.ledger = Ledger(self.db, self.bus)
        self.llm = llm or LLM(cfg)
        self.writer = WritePath(cfg, self.db, self.ledger, self.llm, self.embedder)
        self.verifier = make_verifier(self.llm)
        self.working_sets = ws_mod.WorkingSets()
        self.worker = ThreadPoolExecutor(max_workers=1, thread_name_prefix="rufur-extract")
        self._check_embedder()
        self.ensure_pool(cfg.default_pool, owner=None, description="Default long-term pool")
        self.stm_pool(cfg.default_user)

    def _check_embedder(self) -> None:
        stored = self.db.meta("embedder")
        if stored is None:
            self.db.set_meta("embedder", self.embedder.name)
        elif stored != self.embedder.name:
            raise RuntimeError(f"database vectors come from {stored!r} but the configured embedder is {self.embedder.name!r}")

    def close(self) -> None:
        self.worker.shutdown(wait=True)
        self.db.close()

    # ---------------------------------------------------------------- agents

    def agent(self, name: str) -> dict:
        a = self.db.one("SELECT * FROM agents WHERE name = ?", (name,))
        if a is None:
            self.db.execute(
                "INSERT OR IGNORE INTO agents(id, name, user_id, created_at) VALUES(?,?,?,?)",
                (new_id("agent"), name, self.cfg.default_user, now()),
            )
            a = self.db.one("SELECT * FROM agents WHERE name = ?", (name,))
        self.db.execute("UPDATE agents SET last_seen = ? WHERE name = ?", (now(), name))
        return a

    def create_agent(self, name: str, *, trust: str = "medium_low", clearance: str = "confidential") -> dict:
        if trust not in TRUST_ORDER or clearance not in SENSITIVITY_ORDER:
            raise ValueError("invalid trust or clearance")
        key = "rk_" + secrets.token_urlsafe(24)
        self.agent(name)
        self.db.execute(
            "UPDATE agents SET key_hash = ?, trust = ?, clearance = ? WHERE name = ?", (sha256(key), trust, clearance, name)
        )
        self.ledger.audit(name, "agent.create", name, {"trust": trust, "clearance": clearance})
        return {"name": name, "api_key": key, "trust": trust, "clearance": clearance}

    def agent_for_key(self, key: str) -> str | None:
        return self.db.scalar("SELECT name FROM agents WHERE key_hash = ?", (sha256(key),))

    def list_agents(self) -> list[dict]:
        return self.db.all("SELECT name, user_id, trust, clearance, quarantined, created_at, last_seen FROM agents ORDER BY created_at")

    def quarantine_agent(self, actor: str, name: str) -> dict:
        """Mark an agent compromised and bulk-quarantine everything it authored (plan §8)."""
        self.db.execute("UPDATE agents SET quarantined = 1 WHERE name = ?", (name,))
        rows = self.db.all("SELECT id FROM memories WHERE author_agent = ? AND status IN ('active','disputed')", (name,))
        for r in rows:
            self.ledger.update_memory(r["id"], status="quarantined")
            self.ledger.op("quarantine", r["id"], actor, {"reason": "agent_quarantined"})
        self.ledger.audit(actor, "agent.quarantine", name, {"memories": len(rows)})
        return {"agent": name, "quarantined_memories": len(rows)}

    # ----------------------------------------------------------------- pools

    def ensure_pool(self, name: str, *, owner: str | None, kind: str = "long", description: str | None = None,
                    acl: dict | None = None) -> dict:  # fmt: skip
        p = self.db.one("SELECT * FROM pools WHERE name = ?", (name,))
        if p is None:
            pid = new_id("pool")
            self.db.execute(
                "INSERT INTO pools(id, name, kind, owner, description, retention_days, acl, created_at) VALUES(?,?,?,?,?,?,?,?)",
                (pid, name, kind, owner, description, self.cfg.retention_days,
                 dumps(acl or {"read": ["*"], "write": ["*"]}), now()),  # fmt: skip
            )
            p = self.db.one("SELECT * FROM pools WHERE id = ?", (pid,))
        return p

    def create_pool(self, agent: str, name: str, *, description: str | None = None, acl: dict | None = None) -> dict:
        if self.db.scalar("SELECT 1 FROM pools WHERE name = ?", (name,)):
            raise ValueError(f"pool {name!r} already exists")
        acl = acl or {"read": [agent], "write": [agent]}
        p = self.ensure_pool(name, owner=agent, description=description, acl=acl)
        self.ledger.audit(agent, "pool.create", p["id"], {"acl": acl})
        return self._pool_out(p)

    def set_pool_acl(self, agent: str, name: str, acl: dict) -> dict:
        p = self._pool(name)
        if p["owner"] not in (None, agent):
            raise AccessDenied("only the pool owner can change its ACL")
        self.db.execute("UPDATE pools SET acl = ? WHERE id = ?", (dumps(acl), p["id"]))
        self.ledger.audit(agent, "pool.acl", p["id"], {"acl": acl})
        return self._pool_out(self._pool(name))

    def _pool(self, name_or_id: str) -> dict:
        p = self.db.one("SELECT * FROM pools WHERE name = ? OR id = ?", (name_or_id, name_or_id))
        if p is None:
            raise NotFound(f"pool {name_or_id!r} not found")
        return p

    def _pool_out(self, p: dict) -> dict:
        return {**p, "acl": json.loads(p["acl"]), "memories": self.db.scalar("SELECT count(*) FROM memories WHERE pool_id = ?", (p["id"],))}

    def list_pools(self, agent: str) -> list[dict]:
        a = self.agent(agent)
        pools = self.db.all("SELECT * FROM pools WHERE kind IN ('long', 'stm') AND closed_at IS NULL ORDER BY created_at")
        return [self._pool_out(p) for p in pools if _acl_allows(json.loads(p["acl"]), "read", agent, a["user_id"])]

    def stm_pool(self, user_id: str) -> dict:
        """The user's short-term memory, shared by every agent of that user."""
        principal = f"user:{user_id}"
        return self.ensure_pool(f"stm:{user_id}", owner=principal, kind="stm", description="Short-term memory",
                                acl={"read": [principal], "write": [principal]})  # fmt: skip

    def _readable(self, agent: str) -> tuple[list[str], list[str], list[str]]:
        """(long-term pool ids, STM pool ids, scratch pool ids) this agent may read."""
        a = self.agent(agent)
        long_ids, stm_ids, scratch_ids = [], [], []
        for p in self.db.all("SELECT id, kind, acl FROM pools WHERE closed_at IS NULL"):
            if _acl_allows(json.loads(p["acl"]), "read", agent, a["user_id"]):
                {"stm": stm_ids, "scratch": scratch_ids}.get(p["kind"], long_ids).append(p["id"])
        return long_ids, stm_ids, scratch_ids

    def readable_pools(self, agent: str) -> list[str]:
        long_ids, stm_ids, scratch_ids = self._readable(agent)
        return [*long_ids, *stm_ids, *scratch_ids]

    def _filter(self, agent: str, *, pool: str | None = None, **kw) -> Filter:
        """Everything this agent may see, as one search filter (applied before ranking)."""
        a = self.agent(agent)
        long_ids, stm_ids, scratch_ids = self._readable(agent)
        direct = long_ids + scratch_ids
        if pool:
            only = self._pool(pool)["id"]
            direct = [pid for pid in direct if pid == only]
        kw.setdefault("clearance", a["clearance"])
        kw.setdefault("min_trust", self.cfg.min_trust)
        return Filter(pool_ids=direct, stm_pool_ids=stm_ids, member_pool_ids=scratch_ids,
                      default_target=self._pool(self.cfg.default_pool)["id"], **kw)  # fmt: skip

    def _home_pool(self, agent: str) -> dict:
        """Where an agent's new memories land: its scratch pool if it's a subagent, else its user's STM."""
        a = self.agent(agent)
        if a["user_id"].startswith("scratch:"):
            return self._pool(a["user_id"].split(":", 1)[1])
        return self.stm_pool(a["user_id"])

    def _writable(self, agent: str, pool: str | None) -> dict:
        p = self._pool(pool or self.cfg.default_pool)
        if not _acl_allows(json.loads(p["acl"]), "write", agent, self.agent(agent)["user_id"]):
            raise AccessDenied(f"agent {agent!r} cannot write to pool {p['name']!r}")
        return p

    def _can_read_memory(self, agent: str, mem: dict) -> bool:
        flt = self._filter(agent, statuses=(mem["status"],), min_trust="low", current_only=False)
        where, params = flt.sql()
        return bool(self.db.scalar(f"SELECT 1 FROM memories m WHERE m.id = ? AND {where}", (mem["id"], *params)))

    # -------------------------------------------------------------- sessions

    def session_start(self, agent: str, pool: str | None = None, task_id: str | None = None) -> dict:
        target = self._writable(agent, pool) if pool else self._pool(self.cfg.default_pool)
        sid = new_id("sess")
        # Events and extracted memories go to the agent's home: the user's shared STM (or a scratch pool).
        home = self._home_pool(agent)
        self.db.execute(
            "INSERT INTO sessions(id, agent, pool_id, target_pool, task_id, started_at) VALUES(?,?,?,?,?,?)",
            (sid, agent, home["id"], target["id"], task_id, now()),
        )
        self.ledger.audit(agent, "session.start", sid)
        self.bus.publish("session.started", id=sid, agent=agent)
        return {"session_id": sid, "pool": target["name"]}

    def _session(self, session_id: str) -> dict:
        s = self.db.one("SELECT * FROM sessions WHERE id = ?", (session_id,))
        if s is None:
            raise NotFound(f"session {session_id!r} not found")
        return s

    def log_event(self, agent: str, content: str, *, role: str = "user", session_id: str | None = None,
                  pool: str | None = None, source_type: str | None = None, background: bool = True) -> dict:  # fmt: skip
        if session_id:
            s = self._session(session_id)
            if s["ended_at"]:
                raise ValueError("session already ended")
            pool_id = s["target_pool"]
        else:
            pool_id = self._writable(agent, pool)["id"]
        ev = self.ledger.add_event(pool_id=pool_id, agent=agent, role=role,
                                   source_type=source_type or ROLE_SOURCE.get(role, "tool"),
                                   content=content, session_id=session_id)  # fmt: skip
        checkpoint = False
        if session_id:
            pending = self.db.scalar("SELECT count(*) FROM events WHERE session_id = ? AND extracted = 0", (session_id,))
            if pending >= self.cfg.checkpoint_every:
                checkpoint = True
                self._run(background, self.extract_session, session_id)
        return {"event_id": ev["id"], "checkpoint": checkpoint}

    def _run(self, background: bool, fn, *args):
        if background:
            return self.worker.submit(fn, *args)
        return fn(*args)

    def extract_session(self, session_id: str) -> dict:
        """Checkpoint: extract claims from unextracted events straight into short-term memory."""
        s = self._session(session_id)
        events = self.db.all(
            "SELECT * FROM events WHERE session_id = ? AND extracted = 0 ORDER BY created_at", (session_id,)
        )
        if not events:
            return {"stored": 0, "rejected": 0}
        started = time.perf_counter()
        try:
            accepted, rejected, usage = extract_claims(self.llm, events)
        except LLMUnavailable as e:
            # Leave events unextracted; the next checkpoint or session end retries.
            return {"stored": 0, "rejected": 0, "queued": len(events), "error": str(e)}
        agent = self.agent(s["agent"])
        results = []
        for claim, ev in accepted:
            res = self.writer.commit(_claim_in(claim), pool_id=s["pool_id"], agent=agent, source_type=ev["source_type"],
                                     sources=[(ev["id"], claim.quote)], session_id=session_id, tier="stm",
                                     target_pool=s["target_pool"], candidate_pools=[s["pool_id"], s["target_pool"]])  # fmt: skip
            results.append(res.summary())
        marks = ",".join("?" * len(events))
        self.db.execute(f"UPDATE events SET extracted = 1 WHERE id IN ({marks})", tuple(e["id"] for e in events))
        self.ledger.metric("extract", (time.perf_counter() - started) * 1000, path=usage.model, agent=s["agent"],
                           tokens_in=sum(estimate_tokens(e["content"]) for e in events),
                           llm_tokens=usage.tokens_in + usage.tokens_out,
                           meta={"accepted": len(accepted), "rejected": len(rejected)})  # fmt: skip
        self._guard(s["agent"])
        self._maybe_size_trigger(agent["user_id"])
        return {"stored": sum(r["outcome"] == "created" for r in results), "rejected": len(rejected),
                "model": usage.model, "results": results}  # fmt: skip

    def session_end(self, agent: str, session_id: str, background: bool = False) -> dict:
        s = self._session(session_id)
        if s["agent"] != agent:
            raise AccessDenied("only the session's agent can end it")
        if s["ended_at"]:
            return {"session_id": session_id, "already_ended": True}
        self.db.execute("UPDATE sessions SET ended_at = ? WHERE id = ?", (now(), session_id))
        fut = self._run(background, self._finish_session, session_id)
        return {"session_id": session_id, "scheduled": True} if background else fut

    def _finish_session(self, session_id: str) -> dict:
        """Extract the tail of the session into STM. Filing into long-term stores is consolidation's job."""
        extraction = self.extract_session(session_id)
        self.ledger.audit(self._session(session_id)["agent"], "session.end", session_id,
                          {"stored": extraction.get("stored", 0)})  # fmt: skip
        self.bus.publish("session.ended", id=session_id, stored=extraction.get("stored", 0))
        return {"session_id": session_id, "extraction": extraction}

    # ---------------------------------------------------------------- writes

    def remember(self, agent: str, text: str, *, type: str | None = None, subject: str = "", predicate: str = "",
                 value: str = "", source: str = "agent", pool: str | None = None, sensitivity: str = "internal",
                 pinned: bool = False, session_id: str | None = None, long_term: bool = False) -> dict:  # fmt: skip
        """Explicit write into short-term memory (or a subagent's scratch pool).

        Without a subject/predicate key, the extract model structures the text first. `pool` names the
        long-term pool the memory will be filed into; `long_term=True` files it there immediately.
        """
        started = time.perf_counter()
        a = self.agent(agent)
        home = self._home_pool(agent)
        scratch = home["kind"] == "scratch"
        if scratch and pool:
            raise AccessDenied("a subagent can only write to its own scratch pool")
        target = None if scratch else self._writable(agent, pool)["id"]
        p = home
        source_type = source
        flags_extra: list[str] = []
        if source == "user" and agent != "ui":
            # An agent relaying the user is believed only if a logged user turn contains the text.
            seen = self.db.all(
                "SELECT content FROM events WHERE agent = ? AND role = 'user' AND created_at > ? ORDER BY created_at DESC LIMIT 50",
                (agent, now() - 86400),
            )
            if not any(quote_in(text, e["content"]) or quote_in(value, e["content"]) for e in seen if value or text):
                source_type = "document"  # medium trust: plausible but unverified
                flags_extra.append("unverified_user_claim")
        ev = self.ledger.add_event(pool_id=p["id"], agent=agent, role="user" if source_type == "user" else "assistant",
                                   source_type=source_type, content=text, session_id=session_id)  # fmt: skip
        self.db.execute("UPDATE events SET extracted = 1 WHERE id = ?", (ev["id"],))
        claims: list[ClaimIn] = []
        llm_tokens = 0
        if not (subject and predicate):
            try:
                accepted, _rej, usage = extract_claims(self.llm, [ev])
                llm_tokens = usage.tokens_in + usage.tokens_out
                claims = [_claim_in(c) for c, _ in accepted]
            except LLMUnavailable:
                pass
        if not claims:
            claims = [ClaimIn(type=type or "fact", text=text, subject=subject, predicate=predicate, value=value or text,
                              quote=text, sensitivity=sensitivity)]  # fmt: skip
        results = []
        for c in claims:
            if type:
                c.type = type
            c.pinned = pinned
            if SENSITIVITY_ORDER.index(sensitivity) > SENSITIVITY_ORDER.index(c.sensitivity):
                c.sensitivity = sensitivity  # caller may raise sensitivity, never lower it
            res = self.writer.commit(c, pool_id=p["id"], agent=a, source_type=source_type,
                                     sources=[(ev["id"], c.quote or text)], session_id=session_id, tier="stm",
                                     target_pool=target, candidate_pools=[p["id"]] + ([target] if target else []))  # fmt: skip
            if res.outcome == "created" and flags_extra:
                current = self.ledger.get_memory(res.memory["id"])["flags"]
                self.ledger.update_memory(res.memory["id"], flags=sorted(set(current) | set(flags_extra)))
            llm_tokens += res.llm_tokens
            results.append(res.summary())
        self.ledger.metric("remember", (time.perf_counter() - started) * 1000, path="write", agent=agent,
                           tokens_in=estimate_tokens(text), llm_tokens=llm_tokens)  # fmt: skip
        self._guard(agent)
        out: dict = {"results": results, "event_id": ev["id"]}
        if long_term and not scratch:
            ids = [r["memory"]["id"] for r in results if r["outcome"] == "created" and r["memory"]["status"] != "quarantined"]
            if ids:
                out["consolidation"] = consolidate_mod.consolidate(self, a["user_id"], "agent", memory_ids=ids)
        return out

    def confirm_memory(self, agent: str, memory_id: str, action: str = "confirm") -> dict:
        """User decision on a quarantined or disputed memory."""
        m = self._readable_memory(agent, memory_id)
        if action == "reject":
            self.ledger.update_memory(memory_id, status="rejected", valid_to=now())
            self.ledger.op("reject", memory_id, agent)
            return {"id": memory_id, "status": "rejected"}
        if action != "confirm":
            raise ValueError("action must be 'confirm' or 'reject'")
        superseded = []
        for flag in m["flags"]:
            if flag.startswith("supersede_needs_confirmation:"):
                old = flag.split(":", 1)[1]
                self.ledger.update_memory(old, status="superseded", valid_to=now(), superseded_by=memory_id)
                superseded.append(old)
        if m["status"] == "disputed":
            # Confirming one side of a dispute supersedes the other live versions of the same key.
            for other in self.db.all(
                "SELECT id FROM memories WHERE pool_id = ? AND subject = ? AND predicate = ? AND status = 'disputed' AND id != ?",
                (m["pool_id"], m["subject"], m["predicate"], memory_id),
            ):
                self.ledger.update_memory(other["id"], status="superseded", valid_to=now(), superseded_by=memory_id)
                superseded.append(other["id"])
        flags = [f for f in m["flags"] if not f.startswith("supersede_needs_confirmation:")] + ["user_confirmed"]
        days = consolidate_mod.REVALIDATE_DAYS.get(m["volatility"])
        self.ledger.update_memory(memory_id, status="active", trust="high", flags=flags,
                                  revalidate_at=now() + days * 86400 if days else None)  # fmt: skip
        self.ledger.op("confirm", memory_id, agent, {"superseded": superseded})
        self.ledger.audit(agent, "memory.confirm", memory_id)
        return {"id": memory_id, "status": "active", "superseded": superseded}

    # ----------------------------------------------------------------- reads

    def recall(self, agent: str, query: str, *, token_budget: int | None = None, pool: str | None = None,
               session_id: str | None = None, threshold: float | None = None, verify: bool = True,
               exclude_sent: bool = False, record: bool = True) -> dict:  # fmt: skip
        """Working set → short-term → long-term. `exclude_sent` skips what this session already received."""
        started = time.perf_counter()
        ws = self.working_sets.get(session_id)
        if ws is not None and ws["agent"] != agent:
            ws = None
        if ws and not exclude_sent and threshold is None and (hits := ws_mod.lookup(self, ws, query, self.cfg.verify_band[1])):
            items = hits
            res = {"abstained": False, "missing": None, "items": items, "context": render(items),
                   "tokens": estimate_tokens(render(items)), "path": "working_set",
                   "latency_ms": round((time.perf_counter() - started) * 1000, 2), "used_vector": True}  # fmt: skip
        else:
            flt = self._filter(agent, pool=pool)
            out = fast_recall(self.db, self.embedder, query, flt, token_budget=token_budget or self.cfg.default_token_budget,
                              threshold=self.cfg.abstain_threshold if threshold is None else threshold,
                              half_life_days=self.cfg.recency_half_life_days, k=self.cfg.candidate_k,
                              verifier=self.verifier if (threshold is None and verify) else None,
                              verify_band=tuple(self.cfg.verify_band),
                              exclude=ws["sent"] if (ws and exclude_sent) else None)  # fmt: skip
            res = out.as_dict()
        ids = [it["id"] for it in res["items"]]
        if ws is not None:
            ws["sent"].update(ids)
        stm_hits = [it for it in res["items"] if it.get("tier") == "stm"]
        stm_mod.extend_expiry(self, [it["id"] for it in stm_hits])
        if record:
            self.ledger.metric("recall", res["latency_ms"], path=res["path"], agent=agent,
                               tokens_in=estimate_tokens(query), tokens_out=res["tokens"],
                               meta={"abstained": res["abstained"], "items": len(ids), "query_hash": sha256(query)[:16],
                                     "vector": res["used_vector"],
                                     # Age of short-term items when reused; drives `rufur tune-stm`.
                                     "stm_ages_h": [round((now() - it["created_at"]) / 3600, 2) for it in stm_hits]})  # fmt: skip
            self.ledger.audit(agent, "recall", None, {"items": ids})
            self.bus.publish("recall", agent=agent, items=ids, abstained=res["abstained"])
        return res

    def recall_past(self, agent: str, query: str, *, since: float | None = None, until: float | None = None,
                    include_history: bool = True, token_budget: int | None = None) -> dict:  # fmt: skip
        """Deep look into the past: superseded versions, archived items and daily journals, with time bounds.

        Every item carries its validity window, so old facts are labelled as old, not presented as current.
        """
        statuses = ("active", "disputed", "superseded", "archived") if include_history else ("active", "disputed")
        flt = self._filter(agent, statuses=statuses, current_only=False, since=since, until=until)
        out = fast_recall(self.db, self.embedder, query, flt, token_budget=token_budget or 1500,
                          threshold=self.cfg.abstain_threshold, half_life_days=3650.0, k=self.cfg.candidate_k * 2,
                          verifier=self.verifier, verify_band=tuple(self.cfg.verify_band),
                          header="Memories from the past. Items with until=... are no longer current. " + CONTEXT_HEADER)  # fmt: skip
        res = out.as_dict()
        res["path"] = "past"
        self.ledger.metric("recall", res["latency_ms"], path="past", agent=agent, tokens_in=estimate_tokens(query),
                           tokens_out=res["tokens"], meta={"abstained": res["abstained"], "items": len(res["items"])})  # fmt: skip
        self.ledger.audit(agent, "recall_past", None, {"items": [it["id"] for it in res["items"]]})
        return res

    def bootstrap(self, agent: str, *, project: str | None = None, prompt: str | None = None,
                  session_id: str | None = None, pool: str | None = None) -> dict:  # fmt: skip
        """Start (or reuse) a session and preload its working set; the result is meant for context injection."""
        if not session_id:
            session_id = self.session_start(agent, pool=pool)["session_id"]
        return ws_mod.build(self, agent, session_id, project, prompt)

    def _readable_memory(self, agent: str, memory_id: str) -> dict:
        m = self.ledger.get_memory(memory_id)
        # Same error whether it doesn't exist or the caller can't see it.
        if m is None or not self._can_read_memory(agent, m):
            raise NotFound(f"memory {memory_id!r} not found")
        return m

    def inspect(self, agent: str, memory_id: str) -> dict:
        m = self._readable_memory(agent, memory_id)
        m["sources"] = self.db.all(
            "SELECT s.event_id, s.quote, e.role, e.agent, e.source_type, e.created_at, e.session_id "
            "FROM memory_sources s LEFT JOIN events e ON e.id = s.event_id WHERE s.memory_id = ?",
            (memory_id,),
        )
        m["history"] = self._history(m)
        m["lineage"] = self.db.all("SELECT child_kind, child_id FROM lineage WHERE memory_id = ?", (memory_id,))
        m["related"] = [
            r for r in self.db.all(
                "SELECT id, text, status, trust, source_type, valid_from, valid_to FROM memories "
                "WHERE pool_id = ? AND subject = ? AND predicate = ? AND id != ? AND subject != ''",
                (m["pool_id"], m["subject"], m["predicate"], memory_id),
            )
        ]  # fmt: skip
        m["edges"] = self.db.all("SELECT src, relation, dst FROM edges WHERE memory_id = ?", (memory_id,))
        m["pool"] = self._pool(m["pool_id"])["name"]
        m["retention_score"] = retention_score(m, self.cfg.recency_half_life_days)
        self.ledger.audit(agent, "inspect", memory_id)
        return m

    def _history(self, m: dict) -> list[dict]:
        """The supersede chain this memory belongs to, oldest first."""
        cols = "id, text, status, trust, valid_from, valid_to, superseded_by, source_type"
        chain = [self.db.one(f"SELECT {cols} FROM memories WHERE id = ?", (m["id"],))]
        cur = chain[0]
        while prev := self.db.one(f"SELECT {cols} FROM memories WHERE superseded_by = ?", (cur["id"],)):
            chain.insert(0, prev)
            cur = prev
        cur = chain[-1]
        while cur["superseded_by"] and (nxt := self.db.one(f"SELECT {cols} FROM memories WHERE id = ?", (cur["superseded_by"],))):
            chain.append(nxt)
            cur = nxt
        return chain

    # ---------------------------------------------------------------- forget

    def forget(self, agent: str, *, memory_id: str | None = None, query: str | None = None, confirm: bool = False,
               include_history: bool = True) -> dict:  # fmt: skip
        """By id: forget now. By query: return candidates unless `confirm` (then forget all candidates)."""
        started = time.perf_counter()
        if memory_id:
            ids = [self._readable_memory(agent, memory_id)["id"]]
        elif query:
            flt = self._filter(agent, min_trust="low", statuses=("active", "disputed", "quarantined"))
            res = fast_recall(self.db, self.embedder, query, flt, token_budget=1500, threshold=self.cfg.abstain_threshold,
                              half_life_days=self.cfg.recency_half_life_days, k=self.cfg.candidate_k)  # fmt: skip
            candidates = [{k: it[k] for k in ("id", "text", "type", "status", "relevance")} for it in res.items]
            if not confirm:
                return {"confirm_required": True, "candidates": candidates}
            ids = [c["id"] for c in candidates]
        else:
            raise ValueError("give memory_id or query")
        if not ids:
            return {"forgotten": [], "report": None}
        for mid in ids:
            m = self.ledger.get_memory(mid)
            if m and not self._can_write_pool(agent, m["pool_id"]):
                raise AccessDenied(f"agent {agent!r} cannot forget memories in this pool")
        out = forget_engine.forget(self.cfg, self.db, self.ledger, self.embedder, self.llm, ids, agent, include_history)
        self.ledger.metric("forget", (time.perf_counter() - started) * 1000, path="forget", agent=agent,
                           llm_tokens=(out.get("report") or {}).get("llm_tokens", 0),
                           meta={"count": len(out["forgotten"]),
                                 "completeness": (out.get("report") or {}).get("completeness")})  # fmt: skip
        return out

    def _can_write_pool(self, agent: str, pool_id: str) -> bool:
        p = self._pool(pool_id)
        return _acl_allows(json.loads(p["acl"]), "write", agent, self.agent(agent)["user_id"])

    def _purge_quietly(self, memory_ids: list[str]) -> None:
        """Hard delete without a tombstone: routine expiry or a merge, not a user forget."""
        purge_memories(self.db, memory_ids)
        for mid in memory_ids:
            self.ledger.op("delete", mid, "system", {"reason": "expiry_or_merge"})
            self.bus.publish("memory.deleted", id=mid)

    def auto_forget(self, agent: str = "system") -> dict:
        """Archive low-value memories; hard-delete archived ones past the pool's retention window."""
        archived, deleted = [], []
        for m in self.db.all("SELECT * FROM memories WHERE status = 'active' AND pinned = 0 AND type != 'decision'"):
            if retention_score(m, self.cfg.recency_half_life_days) < self.cfg.archive_below:
                self.ledger.update_memory(m["id"], status="archived")
                self.ledger.op("archive", m["id"], agent)
                archived.append(m["id"])
        for m in self.db.all(
            "SELECT m.id, m.updated_at, p.retention_days FROM memories m JOIN pools p ON p.id = m.pool_id "
            "WHERE m.status = 'archived' AND m.pinned = 0 AND m.type != 'decision'"
        ):
            if m["retention_days"] and now() - m["updated_at"] > m["retention_days"] * 86400:
                deleted.append(m["id"])
        if deleted:
            forget_engine.forget(self.cfg, self.db, self.ledger, self.embedder, self.llm, deleted, agent, verify=False)
        return {"archived": archived, "deleted": deleted}

    # -------------------------------------------------------------- handover

    def handover_create(self, agent: str, task_id: str, packet: ho.HandoverIn) -> dict:
        p = packet.model_dump()
        if not p["memory_ids"]:
            # Gathering context for a task, not answering a question: a looser relevance bar is fine.
            res = self.recall(agent, packet.goal + " " + " ".join(packet.steps_pending), pool=packet.pool,
                              token_budget=1200, threshold=self.cfg.abstain_threshold / 2)  # fmt: skip
            p["memory_ids"] = [it["id"] for it in res["items"]]
        p["memory_ids"] = [mid for mid in p["memory_ids"] if self._safe_readable(agent, mid)]
        p["from_agent"] = agent
        p["created_at"] = now()
        return ho.save(self.db, self.ledger, task_id, p, agent)

    def _safe_readable(self, agent: str, memory_id: str) -> bool:
        m = self.ledger.get_memory(memory_id)
        return bool(m) and self._can_read_memory(agent, m)

    def handover_resume(self, agent: str, task_id: str, token_budget: int | None = None) -> dict:
        t = ho.get(self.db, task_id)
        if t is None:
            raise NotFound(f"task {task_id!r} not found")
        packet = t["packet"]
        visible = [mid for mid in packet.get("memory_ids", []) if self._safe_readable(agent, mid)]
        withheld = len(packet.get("memory_ids", [])) - len(visible)
        next_step = (packet.get("steps_pending") or [packet["goal"]])[0]
        context = self.recall(agent, f"{packet['goal']} {next_step}", token_budget=token_budget, pool=packet.get("pool"))
        ho.mark_resumed(self.db, self.ledger, task_id, agent)
        return {
            "task_id": task_id,
            "from_agent": t["from_agent"],
            "packet": {**packet, "memory_ids": visible},
            "memories": ho.provenance(self.db, visible),
            "withheld_memories": withheld,
            "next_step": next_step,
            "context": context,
        }

    def list_tasks(self, agent: str) -> list[dict]:
        tasks = self.db.all("SELECT * FROM tasks ORDER BY updated_at DESC")
        for t in tasks:
            t["packet"] = json.loads(t["packet"])
        return tasks

    # ------------------------------------------- consolidation, pools, STM

    def consolidate(self, agent: str, *, trigger: str = "agent", journal: bool = False) -> dict:
        """File the user's short-term memories into long-term stores now."""
        a = self.agent(agent)
        if a["user_id"].startswith("scratch:"):
            raise AccessDenied("subagents can't consolidate; close the pool instead")
        day = datetime.fromtimestamp(now()) if journal else None
        return consolidate_mod.consolidate(self, a["user_id"], trigger, journal_day=day)

    def _maybe_size_trigger(self, user_id: str) -> None:
        if user_id.startswith("scratch:"):
            return
        stm = self.stm_pool(user_id)["id"]
        pending = self.db.scalar(
            "SELECT count(*) FROM memories WHERE pool_id = ? AND tier = 'stm' AND consolidation_run IS NULL", (stm,)
        )
        last = self.db.scalar("SELECT max(started_at) FROM consolidation_runs WHERE user_id = ?", (user_id,)) or 0
        events = self.db.scalar("SELECT count(*) FROM events WHERE created_at > ?", (last,))
        if pending > self.cfg.size_trigger_items or events > self.cfg.size_trigger_events:
            self.worker.submit(consolidate_mod.consolidate, self, user_id, "size")

    def consolidation_runs(self, limit: int = 30) -> list[dict]:
        rows = self.db.all("SELECT * FROM consolidation_runs ORDER BY started_at DESC LIMIT ?", (limit,))
        for r in rows:
            r["stats"] = json.loads(r["stats"])
        return rows

    def pool_spawn(self, agent: str, purpose: str, *, needs: list[str] | None = None, memory_ids: list[str] | None = None,
                   ttl_minutes: int | None = None, session_id: str | None = None) -> dict:  # fmt: skip
        if self.agent(agent)["user_id"].startswith("scratch:"):
            raise AccessDenied("a subagent can't spawn further pools")
        return pools_mod.spawn(self, agent, purpose, needs or [], memory_ids or [], ttl_minutes, session_id)

    def pool_close(self, agent: str, pool_id: str, *, summary: str | None = None, reason: str = "closed") -> dict:
        try:
            return pools_mod.close(self, agent, pool_id, summary, reason)
        except LookupError as e:
            raise NotFound(str(e)) from e
        except PermissionError as e:
            raise AccessDenied(str(e)) from e

    def scratch_pools(self, agent: str) -> list[dict]:
        return pools_mod.list_pools(self, agent)

    def subagent_stopped(self, agent: str, session_id: str | None, idle_s: float = 120.0) -> dict:
        """Close this agent's open scratch pools whose subagent has gone quiet (a hook safety net)."""
        closed = []
        for p in self.db.all("SELECT id FROM pools WHERE kind = 'scratch' AND closed_at IS NULL AND owner_agent = ?", (agent,)):
            sub = self.db.one("SELECT last_seen FROM agents WHERE user_id = ?", (f"scratch:{p['id']}",))
            if sub is None or (sub["last_seen"] or 0) < now() - idle_s:
                closed.append(self.pool_close(agent, p["id"], reason="subagent_stopped"))
        return {"closed": closed}

    def sweep(self) -> dict:
        return stm_mod.sweep(self)

    def stm_stats(self) -> dict:
        out = stm_mod.suggest_ttl(self)
        out["items"] = {r["status"]: r["n"] for r in self.db.all(
            "SELECT status, count(*) AS n FROM memories WHERE tier = 'stm' GROUP BY status")}  # fmt: skip
        out["unconsolidated"] = self.db.scalar("SELECT count(*) FROM memories WHERE tier = 'stm' AND consolidation_run IS NULL")
        return out

    def _guard(self, agent: str) -> None:
        """Write-anomaly guard: a burst of writes or repeated attempts to override trusted facts look like a
        compromised or looping agent, so everything it wrote is held for review."""
        a = self.agent(agent)
        if a["quarantined"] or agent == "ui":
            return
        recent = self.db.scalar("SELECT count(*) FROM memories WHERE author_agent = ? AND created_at > ?", (agent, now() - 60))
        overrides = self.db.scalar(
            "SELECT count(*) FROM memories WHERE author_agent = ? AND created_at > ? "
            "AND (flags LIKE '%supersede_needs_confirmation%' OR flags LIKE '%conflicts_with%')",
            (agent, now() - 3600),
        )
        if recent > self.cfg.max_writes_per_min or overrides > self.cfg.max_override_attempts_per_h:
            self.quarantine_agent("guard", agent)
            self.ledger.audit("guard", "agent.auto_quarantine", agent, {"writes_per_min": recent, "overrides_per_h": overrides})

    def doctor(self) -> dict:
        """Consistency between SQL rows, vectors and lineage."""
        return {
            "memories_without_vector": self.db.scalar(
                "SELECT count(*) FROM memories WHERE id NOT IN (SELECT memory_id FROM vec_map WHERE kind = 'memory')"),
            "orphan_vectors": self.db.scalar("SELECT count(*) FROM vec_map WHERE memory_id NOT IN (SELECT id FROM memories)"),
            "orphan_lineage": self.db.scalar("SELECT count(*) FROM lineage WHERE memory_id NOT IN (SELECT id FROM memories)"),
            "memories_without_source": self.db.scalar(
                "SELECT count(*) FROM memories WHERE store != 'journal' AND id NOT IN (SELECT memory_id FROM memory_sources)"),
            "orphan_pool_members": self.db.scalar("SELECT count(*) FROM pool_members WHERE memory_id NOT IN (SELECT id FROM memories)"),
        }  # fmt: skip

    # ------------------------------------------------------- UI / inspection

    def list_memories(self, agent: str, *, pool: str | None = None, status: str | None = None, type: str | None = None,
                      tier: str | None = None, limit: int = 500) -> list[dict]:  # fmt: skip
        statuses = (status,) if status else ("active", "disputed", "quarantined", "superseded", "archived", "rejected")
        flt = self._filter(agent, pool=pool, statuses=statuses, min_trust="low", current_only=False)
        where, params = flt.sql()
        sql = f"SELECT m.*, p.name AS pool FROM memories m JOIN pools p ON p.id = m.pool_id WHERE {where}"
        if type:
            sql += " AND m.type = ?"
            params.append(type)
        if tier:
            sql += " AND m.tier = ?"
            params.append(tier)
        sql += " ORDER BY m.created_at DESC LIMIT ?"
        params.append(limit)
        rows = self.db.all(sql, tuple(params))
        for r in rows:
            r["flags"] = json.loads(r["flags"])
        return rows

    def graph(self, agent: str, *, pool: str | None = None, limit: int = 500) -> dict:
        """Nodes: memories + entities + subjects. Links: relation, supersedes, about, source."""
        mems = self.list_memories(agent, pool=pool, limit=limit)
        ids = {m["id"] for m in mems}
        nodes: list[dict] = []
        links: list[dict] = []
        subjects: dict[str, str] = {}
        for m in mems:
            nodes.append({"id": m["id"], "kind": "memory", "label": m["text"][:80], "type": m["type"], "status": m["status"],
                          "trust": m["trust"], "importance": m["importance"], "pool": m["pool"], "created_at": m["created_at"],
                          "valid_to": m["valid_to"], "author_agent": m["author_agent"], "sensitivity": m["sensitivity"],
                          "tier": m["tier"], "store": m["store"]})  # fmt: skip
            if m["superseded_by"] in ids:
                links.append({"source": m["id"], "target": m["superseded_by"], "kind": "supersedes"})
            if m["subject"]:
                sid = subjects.setdefault(m["subject"], f"subj:{m['subject']}")
                links.append({"source": m["id"], "target": sid, "kind": "about"})
        for name, sid in subjects.items():
            nodes.append({"id": sid, "kind": "subject", "label": name})
        if ids:
            marks = ",".join("?" * len(ids))
            for e in self.db.all(f"SELECT * FROM edges WHERE memory_id IN ({marks})", tuple(ids)):
                for end in (e["src"], e["dst"]):
                    sid = f"subj:{end}"
                    if end not in subjects:
                        subjects[end] = sid
                        nodes.append({"id": sid, "kind": "entity", "label": end})
                links.append({"source": f"subj:{e['src']}", "target": f"subj:{e['dst']}", "kind": "relation",
                              "label": e["relation"], "memory_id": e["memory_id"]})  # fmt: skip
            # Memories that share a source event were learned together.
            rows = self.db.all(f"SELECT memory_id, event_id FROM memory_sources WHERE memory_id IN ({marks})", tuple(ids))
            by_event: dict[str, list[str]] = {}
            for r in rows:
                by_event.setdefault(r["event_id"], []).append(r["memory_id"])
            for group in by_event.values():
                for a, b in zip(group, group[1:]):
                    links.append({"source": a, "target": b, "kind": "co_source"})
        return {"nodes": nodes, "links": links}

    def quarantine(self, agent: str) -> list[dict]:
        rows = self.list_memories(agent, status="quarantined")
        for r in rows:
            r["sources"] = self.db.all("SELECT quote FROM memory_sources WHERE memory_id = ?", (r["id"],))
        return rows

    def metrics_summary(self) -> dict:
        def pct(values: list[float], q: float) -> float:
            if not values:
                return 0.0
            values = sorted(values)
            return round(values[min(len(values) - 1, int(q * len(values)))], 2)

        out: dict[str, Any] = {"ops": {}}
        for op in ("recall", "remember", "extract", "forget"):
            rows = self.db.all("SELECT latency_ms, tokens_in, tokens_out, llm_tokens, meta FROM metrics WHERE op = ?", (op,))
            lat = [r["latency_ms"] for r in rows]
            out["ops"][op] = {
                "count": len(rows),
                "p50_ms": pct(lat, 0.5),
                "p95_ms": pct(lat, 0.95),
                "tokens_out_avg": round(sum(r["tokens_out"] for r in rows) / len(rows), 1) if rows else 0,
                "llm_tokens": sum(r["llm_tokens"] for r in rows),
            }
        recalls = self.db.all("SELECT meta, tokens_out FROM metrics WHERE op = 'recall'")
        out["abstentions"] = sum(1 for r in recalls if json.loads(r["meta"]).get("abstained"))
        # Baseline: what stuffing the full event history into every prompt would have cost.
        history_tokens = sum(estimate_tokens(r["content"]) for r in self.db.all("SELECT content FROM events"))
        out["full_history_tokens"] = history_tokens
        out["avg_recall_tokens"] = out["ops"]["recall"]["tokens_out_avg"]
        out["token_savings_pct"] = (
            round(100 * (1 - out["avg_recall_tokens"] / history_tokens), 1) if history_tokens and recalls else None
        )
        out["counts"] = {
            r["status"]: r["n"] for r in self.db.all("SELECT status, count(*) AS n FROM memories GROUP BY status")
        }
        out["events"] = self.db.scalar("SELECT count(*) FROM events")
        out["tombstones"] = self.db.scalar("SELECT count(*) FROM tombstones")
        out["series"] = self.db.all(
            "SELECT op, created_at, latency_ms, tokens_out FROM metrics ORDER BY id DESC LIMIT 200"
        )
        return out

    def audit_log(self, limit: int = 200) -> list[dict]:
        return self.db.all("SELECT * FROM audit ORDER BY id DESC LIMIT ?", (limit,))

    def event(self, agent: str, event_id: str) -> dict:
        ev = self.db.one("SELECT * FROM events WHERE id = ?", (event_id,))
        if ev is None or (ev["pool_id"] not in self.readable_pools(agent) and ev["agent"] != agent):
            raise NotFound(f"event {event_id!r} not found")
        return ev

    def reconcile_embeddings(self) -> int:
        """Embed memories that were written while the embedder was down."""
        rows = self.db.all(
            "SELECT id, text FROM memories WHERE id NOT IN (SELECT memory_id FROM vec_map WHERE kind = 'memory')"
        )
        done = 0
        for i in range(0, len(rows), 32):
            batch = rows[i : i + 32]
            try:
                vecs = self.embedder.embed([r["text"] for r in batch])
            except EmbedUnavailable:
                break
            for r, v in zip(batch, vecs):
                self.ledger.index_vector("memory", r["id"], r["id"], v)
                done += 1
        return done

    def health(self) -> dict:
        ollama = {"reachable": False, "models": []}
        try:
            import httpx

            r = httpx.get(f"{self.cfg.ollama_url}/api/tags", timeout=2)
            ollama = {"reachable": True, "models": [m["name"] for m in r.json().get("models", [])]}
        except Exception:
            pass
        return {
            "status": "ok",
            "version": __import__("rufur").__version__,
            "home": str(self.cfg.home),
            "memories": self.db.scalar("SELECT count(*) FROM memories"),
            "embedder": self.embedder.name,
            "offline": self.cfg.offline,
            "ollama": ollama,
        }


def _claim_in(claim) -> ClaimIn:
    return ClaimIn(
        type=claim.type,
        text=claim.text,
        subject=claim.subject,
        predicate=claim.predicate,
        value=claim.value,
        quote=claim.quote,
        sensitivity=claim.sensitivity,
        relation=claim.relation.model_dump() if claim.relation else None,
        table_row=claim.table_row.model_dump() if claim.table_row else None,
    )
