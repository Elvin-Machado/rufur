// Thin client for the daemon's REST API. The UI acts as the human user ("ui" agent).

const HEADERS = { "Content-Type": "application/json", "X-Rufur-Agent": "ui" };

async function req<T>(method: string, path: string, body?: unknown): Promise<T> {
  const r = await fetch(path, { method, headers: HEADERS, body: body === undefined ? undefined : JSON.stringify(body) });
  if (!r.ok) {
    let detail = r.statusText;
    try {
      detail = (await r.json()).detail ?? detail;
    } catch {
      /* non-JSON error body */
    }
    throw new Error(`${r.status}: ${detail}`);
  }
  return r.json();
}

export type Status = "active" | "superseded" | "disputed" | "quarantined" | "archived" | "rejected";

export interface GraphNode {
  id: string;
  kind: "memory" | "subject" | "entity";
  label: string;
  type?: string;
  status?: Status;
  trust?: string;
  importance?: number;
  pool?: string;
  created_at?: number;
  valid_to?: number | null;
  author_agent?: string;
  sensitivity?: string;
  tier?: "stm" | "ltm";
  store?: string;
  x?: number;
  y?: number;
}

export interface GraphLink {
  source: string | GraphNode;
  target: string | GraphNode;
  kind: "supersedes" | "about" | "relation" | "co_source";
  label?: string;
}

export interface Memory {
  id: string;
  type: string;
  subject: string;
  predicate: string;
  value: string;
  text: string;
  scope: string | null;
  status: Status;
  trust: string;
  confidence: number;
  importance: number;
  sensitivity: string;
  author_agent: string;
  source_type: string;
  valid_from: number;
  valid_to: number | null;
  superseded_by: string | null;
  created_at: number;
  tier: "stm" | "ltm";
  store: string;
  expires_at: number | null;
  revalidate_at: number | null;
  volatility: string;
  corroboration: number;
  flags: string[];
  pool: string;
  access_count: number;
  retention_score: number;
  sources: { event_id: string; quote: string; role: string; agent: string; source_type: string; created_at: number }[];
  history: { id: string; text: string; status: Status; valid_from: number; valid_to: number | null; source_type: string }[];
  lineage: { child_kind: string; child_id: string }[];
  related: { id: string; text: string; status: Status; trust: string; source_type: string }[];
  edges: { src: string; relation: string; dst: string }[];
}

export interface RecallItem {
  id: string;
  text: string;
  type: string;
  trust: string;
  source_type: string;
  disputed: boolean;
  relevance: number;
  score: number;
  sources: string[];
}

export interface RecallResult {
  abstained: boolean;
  missing: string | null;
  items: RecallItem[];
  context: string;
  tokens: number;
  latency_ms: number;
}

export interface ForgetResult {
  forgotten: { id: string; type: string }[];
  redacted_events: number;
  report: {
    completeness: number;
    probes_passed: number;
    probes_total: number;
    stores: Record<string, { passed: boolean; detail: unknown }>;
    probe_queries: string[];
    note: string;
  } | null;
}

export interface Task {
  id: string;
  title: string | null;
  status: string;
  from_agent: string | null;
  to_agent: string | null;
  updated_at: number;
  packet: {
    goal: string;
    steps_done: string[];
    steps_pending: string[];
    decisions: { decision: string; reason: string }[];
    open_questions: string[];
    memory_ids: string[];
  };
}

export interface Pool {
  id: string;
  name: string;
  memories: number;
  acl: { read: string[]; write: string[] };
}

export const api = {
  graph: (pool?: string) => req<{ nodes: GraphNode[]; links: GraphLink[] }>("GET", `/v1/graph${pool ? `?pool=${encodeURIComponent(pool)}` : ""}`),
  memory: (id: string) => req<Memory>("GET", `/v1/memories/${id}`),
  memories: (q = "") => req<Memory[]>("GET", `/v1/memories${q}`),
  recall: (query: string, pool?: string) => req<RecallResult>("POST", "/v1/recall", { query, pool: pool || null, token_budget: 800 }),
  remember: (text: string, pool?: string) => req<unknown>("POST", "/v1/memories", { text, source: "user", pool: pool || null }),
  confirm: (id: string, action: "confirm" | "reject") => req<unknown>("POST", `/v1/memories/${id}/confirm`, { action }),
  forget: (id: string) => req<ForgetResult>("POST", "/v1/forget", { memory_id: id }),
  quarantine: () => req<(Memory & { sources: { quote: string }[] })[]>("GET", "/v1/quarantine"),
  tasks: () => req<Task[]>("GET", "/v1/tasks"),
  pools: () => req<Pool[]>("GET", "/v1/pools"),
  metrics: () => req<any>("GET", "/v1/metrics/summary"),
  audit: () => req<{ id: number; agent: string; action: string; target: string; created_at: number }[]>("GET", "/v1/audit?limit=60"),
  health: () => req<any>("GET", "/v1/health"),
  consolidate: () => req<any>("POST", "/v1/consolidate", { journal: false }),
  runs: () => req<{ id: string; trigger: string; started_at: number; finished_at: number | null; stats: any }[]>("GET", "/v1/consolidation/runs"),
  stmStats: () => req<any>("GET", "/v1/stm/stats"),
  scratchPools: () => req<any[]>("GET", "/v1/scratch-pools"),
};

export function until(ts: number): string {
  const s = ts - Date.now() / 1000;
  if (s <= 0) return "now";
  if (s < 3600) return `in ${Math.ceil(s / 60)} min`;
  if (s < 86400) return `in ${Math.round(s / 3600)} h`;
  return `in ${Math.round(s / 86400)} days`;
}

export const STORE_LABEL: Record<string, string> = {
  profile: "user profile",
  fact: "facts",
  table: "a table",
  graph: "the relation graph",
  procedure: "procedures",
  journal: "the daily journal",
  discard: "nowhere (short-term only)",
};

export function since(ts: number): string {
  const s = Date.now() / 1000 - ts;
  if (s < 60) return "just now";
  if (s < 3600) return `${Math.floor(s / 60)} min ago`;
  if (s < 86400) return `${Math.floor(s / 3600)} h ago`;
  return new Date(ts * 1000).toLocaleDateString(undefined, { day: "numeric", month: "short" });
}

export const SOURCE_LABEL: Record<string, string> = {
  user: "said by the user",
  document: "from a document",
  agent: "inferred by an agent",
  tool: "from tool output",
  web: "from the web",
};
