# RUFUR: Knowledge Brain

A memory layer that any agent can use through **MCP** or **REST**. Every memory traces back to its source quote and time. RUFUR handles updates and contradictions, quarantines planted facts, says "I don't know", forgets on request and proves the deletion, and lets agents and subagents share memory safely.

It runs as an Ollama-style local daemon on `127.0.0.1:7437`, which serves the REST API, the MCP endpoint and a visual brain UI. All LLM work runs on **local Ollama models**, with free cloud models as a fallback.

## How memory flows

```
 conversation ──▶ short-term memory (STM) ──consolidation──▶ long-term memory (LTM)
 (any agent)      shared by all your agents,                   profile · facts · tables · graph ·
                  lives ~3 days                                procedures · daily journal
```

- **Short-term memory.** Everything any of your agents (Claude Code, opencode, …) captures goes into one user-scoped STM.
  - Trust, quarantine and conflict checks apply immediately.
  - An item lives 72 h; each use keeps it alive another 24 h, up to 7 days.
  - `rufur tune-stm` suggests a better lifetime from how long items are actually reused.
- **Consolidation** runs when an agent calls `consolidate`, nightly at 03:00 (or on daemon start if the last run was missed), and when STM gets large.
  - An LLM proposes which store each item belongs in. Rules validate the proposal; anything invalid falls back to fixed rules.
  - Items are promoted **in place**: same id, same sources, same lineage.
  - Each night also writes a journal entry for the day.
- **Retrieval** is tiered:
  1. the session's **working set**, preloaded at session start
  2. short-term memory
  3. long-term hybrid search
  4. `recall_past`, for superseded versions, journals and time ranges
- **Every row is traceable.** SQL rows and their vectors live in one SQLite file and are written in one transaction. They are linked by `vec_map` and `lineage`, and each row carries its source event, verbatim quote, agent, time, validity window and trust. `rufur doctor` checks the links.
- **Subagents** get a private pool from `pool_spawn`, seeded by reference with only what they need. `pool_close` files their useful findings back into STM and discards the rest.

## How it handles stale, contradictory and planted memories

- **Stale:**
  - Every memory records when it was learned and when it was true. An update closes the old version instead of deleting it.
  - Changeable facts get a revalidation date. After it passes, the fact is still returned but tagged `stale="needs confirmation"` and ranked lower.
- **Contradictory:** RUFUR settles what it can and hands the rest to the calling agent. There is no UI step.
  1. Rules: a tool or web claim never beats an existing memory, an earlier user confirmation wins, and clearly better-corroborated claims win. An LLM label of "update" counts only when the *source text* signals a change.
  2. The memory layer's own LLM re-reads both source messages. Its verdict counts only if it's confident, and it can never hand the win to a less trusted source.
  3. Otherwise the calling agent decides. Recall results carry `decisions_needed` (also in `pending_decisions`), and the agent answers with `resolve_conflict`. It asks the user only when `needs_user` is true, meaning a less trusted source would replace something the user said, and RUFUR accepts `decided_by="user"` only if the user just wrote to that agent.
  4. Nothing waits forever: after 7 days the more trusted (then newer) version wins, and the loser stays in history.
- **Planted:**
  - Trust comes from where the quote physically sits (user turn, document, tool or web output). The LLM can never raise it.
  - Low-trust and instruction-like content is quarantined until an independent source corroborates it or the user approves it.
  - An agent that writes in bursts or keeps trying to override trusted facts is quarantined automatically.
  - Retrieved memories are always wrapped as data, never instructions.

## How it forgets, and proves it

- **Scope first.** A request like "forget my phone number" matches exact keys before fuzzy search, so it never resolves to a neighbour. It takes every version of that fact and verbatim copies. Other memories that mention the value are listed for the user to confirm (`cascade_mentions`).
- **Erase in time proportional to what's deleted.** SQLite `secure_delete`, FTS5 `secure-delete` and a WAL truncate zero the bytes at delete time. Events and handover packets are redacted, and conflicts, journals and lineage rows are dropped. A full VACUUM runs only if the byte scan still finds a stale copy on disk.
- **Probe before and after.** Each probe (id lookups in every table, text scans of every store, recall through every read path, a nearest-neighbour search with the memory's own vector, a raw byte scan of the database, WAL and files) must find the content *before* the delete and nothing after. Probes that couldn't see it beforehand are reported as uninformative, not counted as passes.
- **Signed receipt.** Each forget issues a receipt with salted hashes, ids and probe outcomes, never the content. It is HMAC-signed with a key kept outside the database and hash-chained to the previous one. `rufur verify-forgetting` (MCP `verify_forgetting`) re-checks the whole chain, confirms the ids are still gone, and that nothing hashing to a tombstone has come back. The nightly sweeper runs the same re-check.
- **Too private to read is not too private to forget.** If the user asks an agent directly, it can forget a memory above its clearance; the agent sees only `[restricted memory, text hidden]`.
- `scripts/forget_bench.py`: 20/20 deletion requests completed end to end at 1k, 5k and 20k memories, checked by a different agent through recall, recall_past and a handover. Erasure takes 3–24 ms, against 10–167 ms for the per-forget VACUUM it replaced.

## Try it

[`docs/TRY_IT.md`](docs/TRY_IT.md) walks through every feature hands-on with copy-paste commands against a throwaway daemon, plus Claude Code and opencode.

## Quick start

```bash
# prerequisites: uv, Ollama running, and these models pulled
ollama pull qwen2.5:7b-instruct && ollama pull nomic-embed-text && ollama pull qwen3.5:4b

uv sync
cd ui && npm install && npm run build && cd ..   # builds the brain UI into src/rufur/static

uv run rufur start      # background daemon (also runs the STM sweeper and nightly consolidation)
uv run rufur status     # daemon, Ollama, and which model each role will use
uv run rufur open       # open the brain UI
```

Other commands:
- `rufur serve` runs the daemon in the foreground; `rufur stop` and `rufur logs` manage the background one.
- `rufur consolidate [--journal]`, `rufur sweep`, `rufur tune-stm` and `rufur doctor` run maintenance by hand.
- `rufur agent add NAME` issues an API key for an agent.
- `rufur install-service` registers a launchd agent so the daemon starts at login.

## Use it from Claude Code (plugin)

The plugin makes Claude Code use RUFUR without being told:
- At session start it loads your working memory (profile, recent context, project-relevant facts) into context.
- On every prompt it adds relevant memories that weren't already sent.
- It records the conversation.
- When a subagent stops, it closes that subagent's pool.
- It ships the `rufur-memory` skill, which teaches the workflow, and registers the MCP tools.

```
/plugin marketplace add /path/to/rufur
/plugin install rufur@rufur
```

The hooks talk to `http://127.0.0.1:7437`. To use another URL or identity, set `RUFUR_URL`, `RUFUR_AGENT` or `RUFUR_API_KEY`. Every hook fails open: if RUFUR is down, Claude Code carries on.

## Use it from opencode

Merge `integrations/opencode/opencode.json` into your opencode config, which adds the `rufur` MCP server as agent `opencode`, and add `integrations/opencode/AGENTS.md` to your instructions. opencode shares the same short-term memory as Claude Code. It doesn't have the automatic hooks, so the instructions tell it when to call the tools.

## Use it from pi

pi has no MCP, so it reaches RUFUR through a skill with a small CLI, plus an extension that handles recall and capture automatically:

```bash
cp integrations/pi/extensions/rufur.ts ~/.pi/agent/extensions/          # auto recall + capture, agent "pi"
cp -r integrations/pi/skills/rufur-memory ~/.pi/agent/skills/            # skill + scripts/rufur.py
```

- **The extension:**
  - loads the working set on your first prompt
  - adds relevant memories on every prompt
  - records your prompts and pi's replies
  - ends the RUFUR session when the pi session ends
  - skips `--mode json` runs, which is how pi subagents are spawned
- **The skill** teaches pi to run `scripts/rufur.py`, which covers recall, past, remember, decisions and resolve, consolidate, forget, spawn and close for subagent pools, and handover and resume.
- **The skill works without the extension**, but then pi only uses memory when it decides to.

## MCP tools and REST endpoints

| MCP tool | REST |
|---|---|
| `remember` (`long_term=true` files it immediately) | `POST /v1/memories` |
| `recall` | `POST /v1/recall` |
| `recall_past` | `POST /v1/recall/past` |
| `inspect` | `GET /v1/memories/{id}` |
| `confirm_memory` | `POST /v1/memories/{id}/confirm` |
| `forget` | `POST /v1/forget` |
| `consolidate` | `POST /v1/consolidate` |
| `pending_decisions` / `resolve_conflict` | `GET /v1/decisions`, `POST /v1/decisions/{id}` |
| `pool_spawn` / `pool_close` | `POST /v1/pools/spawn`, `POST /v1/pools/{id}/close` |
| `log_event`, `session_start` / `session_end` | `POST /v1/events`, `POST /v1/sessions`, `POST /v1/sessions/{id}/end` |
| `handover_create` / `handover_resume` | `POST /v1/tasks/{id}/handover`, `POST /v1/tasks/{id}/resume` |
| `list_pools` | `GET /v1/pools` |

Endpoints used by the hooks and the UI:
- `POST /v1/bootstrap` (working set)
- `GET /v1/graph`, `/v1/quarantine`, `/v1/metrics/summary`, `/v1/audit`
- `/v1/consolidation/runs`, `/v1/stm/stats`, `/v1/scratch-pools`, `/v1/doctor`
- `/v1/stream` (server-sent events)

Interactive API docs are at `/docs`.

**Identity:**
- Send `Authorization: Bearer <key>` (from `rufur agent add`) or, when `require_auth` is off, `X-Rufur-Agent: <name>`.
- Agents belong to a user (default `me`) and share that user's STM.
- Pool ACLs and sensitivity clearance are checked before ranking, and a subagent's key sees only its own pool.

## Models

Each LLM role has a provider chain set in `~/.rufur/config.toml`:

| Role | Default chain |
|---|---|
| extract, classify, consolidate, resolve, verify | `qwen2.5:7b-instruct` → `gpt-oss:120b-cloud` (Ollama cloud) → Groq `llama-3.3-70b-versatile` |
| probe (forget verification) | `qwen3.5:4b` → `qwen2.5:7b-instruct` |
| embeddings | `nomic-embed-text` through Ollama |

Groq is used only when `GROQ_API_KEY` is set. STM lifetimes, the nightly hour and the guard limits are set in the same file (see `src/rufur/config.py`).

## Development

```bash
uv run pytest                                  # offline: fake LLM, hashing embedder, adjustable test clock
uv run python scripts/demo_scenario.py         # end to end against the running daemon (real models)
uv run python scripts/baseline_compare.py      # full history vs plain vector RAG vs RUFUR
uv run python scripts/llm_bakeoff.py           # compare local models for extraction
cd ui && npm run dev                           # UI dev server, proxies /v1 to the daemon
```

Set `RUFUR_HOME` to use a separate data directory and `RUFUR_PORT` to use another port.
