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
- **Contradictory:**
  - An LLM label of "update" is accepted only when the *source text* signals a change.
  - Otherwise, the more trusted source wins, the user's own confirmation beats everything, and equal-trust conflicts are returned as **disputed** for the user to settle.
  - A nightly sweep catches conflicts between agents.
- **Planted:**
  - Trust comes from where the quote physically sits (user turn, document, tool or web output). The LLM can never raise it.
  - Low-trust and instruction-like content is quarantined until an independent source corroborates it or the user approves it.
  - An agent that writes in bursts or keeps trying to override trusted facts is quarantined automatically.
  - Retrieved memories are always wrapped as data, never instructions.

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
| extract, classify, consolidate, verify | `qwen2.5:7b-instruct` → `gpt-oss:120b-cloud` (Ollama cloud) → Groq `llama-3.3-70b-versatile` |
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
