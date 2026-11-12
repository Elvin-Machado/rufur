import { FormEvent, useCallback, useEffect, useMemo, useRef, useState } from "react";
import { api, GraphLink, GraphNode, Pool, RecallResult } from "./api";
import BrainGraph from "./BrainGraph";
import Inspector from "./Inspector";
import { Metrics, Review, Tasks, Timeline } from "./Views";

type View = "brain" | "timeline" | "review" | "tasks" | "metrics";

const VIEWS: { id: View; label: string }[] = [
  { id: "brain", label: "Brain" },
  { id: "timeline", label: "Timeline" },
  { id: "review", label: "Review" },
  { id: "tasks", label: "Agents" },
  { id: "metrics", label: "Usage" },
];

export default function App() {
  const [view, setView] = useState<View>("brain");
  const [pool, setPool] = useState<string>("");
  const [pools, setPools] = useState<Pool[]>([]);
  const [graph, setGraph] = useState<{ nodes: GraphNode[]; links: GraphLink[] }>({ nodes: [], links: [] });
  const [selected, setSelected] = useState<string | null>(null);
  const [version, setVersion] = useState(0);
  const [live, setLive] = useState(false);
  const [reviewCount, setReviewCount] = useState(0);

  const [mode, setMode] = useState<"ask" | "remember">("ask");
  const [query, setQuery] = useState("");
  const [asking, setAsking] = useState(false);
  const [answer, setAnswer] = useState<RecallResult | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [replay, setReplay] = useState<number | null>(null);

  const refresh = useCallback(() => setVersion((v) => v + 1), []);

  useEffect(() => {
    api.graph(pool || undefined).then(setGraph);
    api.pools().then(setPools);
    Promise.all([api.quarantine(), api.memories("?status=disputed")]).then(([q, d]) => setReviewCount(q.length + d.length));
  }, [version, pool]);

  // Live updates: any write, forget or handover anywhere refreshes the views.
  const timer = useRef<number>();
  useEffect(() => {
    const es = new EventSource("/v1/stream");
    es.onopen = () => setLive(true);
    es.onerror = () => setLive(false);
    es.onmessage = (e) => {
      const ev = JSON.parse(e.data);
      if (ev.type === "recall" || ev.type === "event.created") return;
      window.clearTimeout(timer.current);
      timer.current = window.setTimeout(refresh, 400);
    };
    return () => es.close();
  }, [refresh]);

  async function submit(e: FormEvent) {
    e.preventDefault();
    const q = query.trim();
    if (!q) return;
    setAsking(true);
    setNotice(null);
    try {
      if (mode === "ask") {
        setAnswer(await api.recall(q, pool || undefined));
        setView("brain");
      } else {
        await api.remember(q, pool || undefined);
        setNotice("Saved. It appears in the brain once it’s been checked against what’s already there.");
        setQuery("");
        refresh();
      }
    } catch (err: any) {
      setNotice(err.message);
    } finally {
      setAsking(false);
    }
  }

  const highlight = useMemo(() => new Set(answer?.items.map((i) => i.id) ?? []), [answer]);
  const memNodes = graph.nodes.filter((n) => n.kind === "memory");
  const times = memNodes.map((n) => n.created_at ?? 0).sort((a, b) => a - b);
  const counts = memNodes.reduce<Record<string, number>>((acc, n) => ({ ...acc, [n.status!]: (acc[n.status!] ?? 0) + 1 }), {});

  return (
    <div className="app">
      <header className="top">
        <div className="brand">
          <span className="mark" aria-hidden />
          <span>RUFUR</span>
          <span className={`live ${live ? "on" : ""}`} title={live ? "Receiving live updates" : "Not connected to the daemon"}>
            {live ? "live" : "offline"}
          </span>
        </div>
        <form className="ask" onSubmit={submit}>
          <div className="mode" role="radiogroup" aria-label="What to do">
            <button type="button" role="radio" aria-checked={mode === "ask"} className={mode === "ask" ? "on" : ""} onClick={() => setMode("ask")}>
              Ask
            </button>
            <button type="button" role="radio" aria-checked={mode === "remember"} className={mode === "remember" ? "on" : ""} onClick={() => setMode("remember")}>
              Remember
            </button>
          </div>
          <input
            value={query}
            onChange={(e) => setQuery(e.target.value)}
            placeholder={mode === "ask" ? "Ask what your agents would recall, e.g. where does the user live?" : "Tell the brain something to remember"}
            aria-label={mode === "ask" ? "Question" : "Memory to save"}
          />
          <button className="primary" disabled={asking || !query.trim()}>
            {asking ? "…" : mode === "ask" ? "Ask" : "Save"}
          </button>
        </form>
        <label className="pool">
          <span>Pool</span>
          <select value={pool} onChange={(e) => setPool(e.target.value)}>
            <option value="">All pools</option>
            {pools.map((p) => (
              <option key={p.id} value={p.name}>
                {p.name} ({p.memories})
              </option>
            ))}
          </select>
        </label>
      </header>

      <nav className="views" aria-label="Views">
        {VIEWS.map((v) => (
          <button key={v.id} className={view === v.id ? "on" : ""} aria-current={view === v.id ? "page" : undefined} onClick={() => setView(v.id)}>
            {v.label}
            {v.id === "review" && reviewCount > 0 && <span className="badge">{reviewCount}</span>}
          </button>
        ))}
      </nav>

      <main className={`main ${selected ? "with-drawer" : ""}`}>
        {view === "brain" && (
          <div className="brain">
            {memNodes.length === 0 ? (
              <div className="blank">
                <p>The brain is empty.</p>
                <p className="muted">Connect Claude Code or another agent over MCP, or switch the box above to Remember and add something.</p>
              </div>
            ) : (
              <BrainGraph nodes={graph.nodes} links={graph.links} highlight={highlight} selected={selected} onSelect={setSelected} upTo={replay} />
            )}

            <div className="legend" aria-label="Legend">
              <span><i className="dot active" />long-term {memNodes.filter((n) => n.status === "active" && n.tier !== "stm").length}</span>
              <span><i className="dot stm" />short-term {memNodes.filter((n) => n.status === "active" && n.tier === "stm").length}</span>
              <span><i className="dot disputed" />disputed {counts.disputed ?? 0}</span>
              <span><i className="dot quarantined" />held for review {counts.quarantined ?? 0}</span>
              <span><i className="dot superseded" />replaced {counts.superseded ?? 0}</span>
            </div>

            {times.length > 1 && (
              <label className="replay">
                <span>{replay === null ? "Showing everything" : `As of ${new Date(replay * 1000).toLocaleString(undefined, { dateStyle: "medium", timeStyle: "short" })}`}</span>
                <input
                  type="range"
                  min={times[0]}
                  max={times[times.length - 1]}
                  step={(times[times.length - 1] - times[0]) / 200 || 1}
                  value={replay ?? times[times.length - 1]}
                  onChange={(e) => {
                    const v = Number(e.target.value);
                    setReplay(v >= times[times.length - 1] ? null : v);
                  }}
                  aria-label="Replay how the brain grew"
                />
              </label>
            )}

            {answer && (
              <section className="answer" aria-live="polite">
                <header>
                  <h2>
                    {answer.abstained
                      ? "Nothing stored answers this"
                      : `${answer.items.length} ${answer.items.length === 1 ? "memory" : "memories"} would be sent`}
                  </h2>
                  <button className="close" onClick={() => setAnswer(null)} aria-label="Close answer">
                    ×
                  </button>
                </header>
                {answer.abstained ? (
                  <p className="muted">Agents get an honest “I don’t know” instead of a guess.</p>
                ) : (
                  <ol>
                    {answer.items.map((it) => (
                      <li key={it.id}>
                        <button className="link mem-line" onClick={() => setSelected(it.id)}>
                          {it.text}
                        </button>
                        <span className="muted small">
                          {it.trust.replace("_", " ")} trust, match {Math.round(it.relevance * 100)}%{it.disputed && <span className="chip s-disputed">disputed</span>}
                        </span>
                      </li>
                    ))}
                  </ol>
                )}
                <p className="muted small">
                  {answer.tokens} tokens, {Math.round(answer.latency_ms)} ms
                </p>
              </section>
            )}
          </div>
        )}
        {view === "timeline" && <Timeline onSelect={setSelected} version={version} />}
        {view === "review" && <Review onSelect={setSelected} version={version} onChanged={refresh} />}
        {view === "tasks" && <Tasks onSelect={setSelected} version={version} />}
        {view === "metrics" && <Metrics version={version} onChanged={refresh} />}

        {selected && <Inspector id={selected} onClose={() => setSelected(null)} onChanged={refresh} onSelect={setSelected} />}
      </main>

      {notice && (
        <div className="toast" role="status" onClick={() => setNotice(null)}>
          {notice}
        </div>
      )}
    </div>
  );
}
