/**
 * opencode ↔ RUFUR plugin: automatic recall and capture (the opencode equivalent of the Claude Code hooks).
 *
 *   first user message  start a RUFUR session and preload the working set (profile, recent, project facts)
 *   every user message  capture it; recall relevant memories not already sent this session
 *   every model call    add the working set + this session's recalled memories to the system prompt
 *   webfetch/websearch  capture their output as low-trust web content
 *   session idle        capture the assistant's latest reply
 *   session deleted     end the RUFUR session (extraction runs in the daemon's background)
 *
 * Subagent sessions (opencode `task` children) are not captured or preloaded: their "user" messages are
 * written by the parent agent, not the human, and they should get memory through pool_spawn instead.
 *
 * Install: copy to ~/.config/opencode/plugins/rufur.ts (or <project>/.opencode/plugins/). Everything
 * fails open: if RUFUR is down, opencode works normally.
 * Env: RUFUR_URL (default http://127.0.0.1:7437), RUFUR_API_KEY or RUFUR_AGENT (default "opencode"),
 * RUFUR_PROMPT_BUDGET (default 300), RUFUR_CAPTURE_TOOLS (default "webfetch,websearch").
 */
import type { Plugin } from "@opencode-ai/plugin"
import { basename } from "node:path"

const BASE = (process.env.RUFUR_URL ?? "http://127.0.0.1:7437").replace(/\/$/, "")
const AGENT = process.env.RUFUR_AGENT ?? "opencode"
const KEY = process.env.RUFUR_API_KEY
const BUDGET = Number(process.env.RUFUR_PROMPT_BUDGET ?? 300)
const CAPTURE_TOOLS = new Set((process.env.RUFUR_CAPTURE_TOOLS ?? "webfetch,websearch").split(",").filter(Boolean))
const MAX_CHARS = 4000
const KEEP_RECALLS = 5 // recalled blocks kept in the system prompt per session

type State = {
  rufur?: string // RUFUR session id
  child?: boolean // an opencode subagent session
  base: string // working set injected at session start
  recalls: string[] // memories recalled for recent prompts
  lastAssistant?: string // id of the last captured assistant message
}

async function call(path: string, body: unknown = {}, timeoutMs = 3000): Promise<any> {
  const headers: Record<string, string> = { "content-type": "application/json" }
  if (KEY) headers.authorization = `Bearer ${KEY}`
  else headers["x-rufur-agent"] = AGENT
  const r = await fetch(BASE + path, { method: "POST", headers, body: JSON.stringify(body), signal: AbortSignal.timeout(timeoutMs) })
  if (!r.ok) throw new Error(`rufur ${path}: ${r.status}`)
  return r.json()
}

const note = (sid: string) =>
  `RUFUR memory is active (session ${sid}). Relevant memories are added to your context automatically; use the ` +
  `rufur MCP tools for more (recall, recall_past) and to save decisions (remember). Load the rufur-memory skill for ` +
  `the full workflow. Before delegating to a subagent, call pool_spawn with session_id="${sid}" and hand it the returned key.`

export const RufurPlugin: Plugin = async ({ client, directory }) => {
  const sessions = new Map<string, State>()
  const state = (id: string) => sessions.get(id) ?? sessions.set(id, { base: "", recalls: [] }).get(id)!
  const quietly = async <T>(fn: () => Promise<T>): Promise<T | undefined> => {
    try {
      return await fn()
    } catch {
      return undefined // memory must never break the user's session
    }
  }

  async function ensure(sessionID: string, prompt?: string): Promise<State> {
    const s = state(sessionID)
    if (s.rufur || s.child) return s
    const info = await quietly(() => client.session.get({ path: { id: sessionID } }))
    if (info?.data?.parentID) {
      s.child = true
      return s
    }
    const out = await call("/v1/bootstrap", { project: basename(directory), prompt }, 6000)
    s.rufur = out.session_id
    s.base = [note(out.session_id), out.context].filter(Boolean).join("\n\n")
    return s
  }

  async function end(s: State | undefined) {
    if (s?.rufur) await quietly(() => call(`/v1/sessions/${s.rufur}/end?background=true`, {}, 1500))
  }

  return {
    "chat.message": async (input, output) => {
      const text = output.parts
        .filter((p: any) => p.type === "text" && !p.synthetic && !p.ignored)
        .map((p: any) => p.text)
        .join("\n")
        .trim()
      if (!text) return
      await quietly(async () => {
        const s = await ensure(input.sessionID, text)
        if (!s.rufur) return
        await call("/v1/events", { content: text.slice(0, MAX_CHARS), role: "user", session_id: s.rufur })
        const r = await call(
          "/v1/recall",
          { query: text.slice(0, 1000), session_id: s.rufur, token_budget: BUDGET, verify: false, exclude_sent: true },
          2500,
        )
        if (!r.abstained && r.items?.length) s.recalls = [...s.recalls, r.context].slice(-KEEP_RECALLS)
      })
    },

    "experimental.chat.system.transform": async (input, output) => {
      const s = input.sessionID ? sessions.get(input.sessionID) : undefined
      if (!s?.rufur) return
      if (s.base) output.system.push(s.base)
      output.system.push(...s.recalls)
    },

    "tool.execute.after": async (input, output) => {
      if (!CAPTURE_TOOLS.has(input.tool)) return
      await quietly(async () => {
        const s = await ensure(input.sessionID)
        if (!s.rufur) return
        const text = `[${input.tool}] ${output.output ?? ""}`.slice(0, MAX_CHARS)
        await call("/v1/events", { content: text, role: "web", session_id: s.rufur })
      })
    },

    event: async ({ event }) => {
      if (event.type === "session.idle") {
        const s = sessions.get(event.properties.sessionID)
        if (!s?.rufur) return
        await quietly(async () => {
          const res = await client.session.messages({ path: { id: event.properties.sessionID } })
          const last = [...(res.data ?? [])].reverse().find((m: any) => m.info.role === "assistant")
          if (!last || last.info.id === s.lastAssistant) return
          const text = last.parts
            .filter((p: any) => p.type === "text" && !p.synthetic)
            .map((p: any) => p.text)
            .join("\n")
            .trim()
          s.lastAssistant = last.info.id
          if (text) await call("/v1/events", { content: text.slice(0, MAX_CHARS), role: "assistant", session_id: s.rufur })
        })
      } else if (event.type === "session.deleted") {
        const id = event.properties.info.id
        await end(sessions.get(id))
        sessions.delete(id)
      }
    },

    dispose: async () => {
      await Promise.all([...sessions.values()].map(end))
    },
  }
}
