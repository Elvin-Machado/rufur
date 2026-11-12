import { useEffect, useState } from "react";
import { api, Memory, since, SOURCE_LABEL, STORE_LABEL, Task, until } from "./api";

type Select = (id: string) => void;

export function Timeline({ onSelect, version }: { onSelect: Select; version: number }) {
  const [mems, setMems] = useState<Memory[]>([]);
  useEffect(() => {
    api.memories("?limit=300").then(setMems);
  }, [version]);
  const byDay = new Map<string, Memory[]>();
  mems.forEach((m) => {
    const day = new Date(m.created_at * 1000).toLocaleDateString(undefined, { weekday: "short", day: "numeric", month: "short" });
    byDay.set(day, [...(byDay.get(day) ?? []), m]);
  });
  const textOf = new Map(mems.map((m) => [m.id, m.text]));
  if (!mems.length) return <Empty text="Nothing remembered yet. Connect an agent or add a memory with the box above." />;
  return (
    <div className="pane timeline">
      {[...byDay.entries()].map(([day, items]) => (
        <section key={day}>
          <h3>{day}</h3>
          <ol>
            {items.map((m) => (
              <li key={m.id} className={`t-${m.status}`}>
                <time>{new Date(m.created_at * 1000).toLocaleTimeString(undefined, { hour: "2-digit", minute: "2-digit" })}</time>
                <div>
                  <button className="link mem-line" onClick={() => onSelect(m.id)}>
                    {m.text}
                  </button>
                  <p className="muted small">
                    {m.type}, {SOURCE_LABEL[m.source_type] ?? m.source_type}, by {m.author_agent}
                    {m.status !== "active" && <span className={`chip s-${m.status}`}>{m.status}</span>}
                    {m.status === "superseded" && m.superseded_by && textOf.get(m.superseded_by) && (
                      <> replaced by “{textOf.get(m.superseded_by)}”</>
                    )}
                  </p>
                </div>
              </li>
            ))}
          </ol>
        </section>
      ))}
    </div>
  );
}

export function Review({ onSelect, version, onChanged }: { onSelect: Select; version: number; onChanged: () => void }) {
  const [items, setItems] = useState<Awaited<ReturnType<typeof api.quarantine>>>([]);
  const [disputed, setDisputed] = useState<Memory[]>([]);
  useEffect(() => {
    api.quarantine().then(setItems);
    api.memories("?status=disputed").then(setDisputed);
  }, [version]);

  async function decide(id: string, action: "confirm" | "reject") {
    await api.confirm(id, action);
    onChanged();
  }

  if (!items.length && !disputed.length) return <Empty text="Nothing to review. Suspicious or conflicting memories will show up here." />;
  return (
    <div className="pane review">
      {items.length > 0 && (
        <section>
          <h2>Held for review</h2>
          <p className="muted">These came from low-trust sources or tried to give instructions. Agents can’t see them until you approve.</p>
          <ul>
            {items.map((m) => (
              <li key={m.id}>
                <button className="link mem-line" onClick={() => onSelect(m.id)}>
                  {m.text}
                </button>
                {m.sources[0] && <blockquote>{m.sources[0].quote}</blockquote>}
                <p className="muted small">
                  {SOURCE_LABEL[m.source_type] ?? m.source_type}, by {m.author_agent}, {since(m.created_at)}
                </p>
                <div className="row">
                  <button className="primary" onClick={() => decide(m.id, "confirm")}>
                    Approve
                  </button>
                  <button onClick={() => decide(m.id, "reject")}>Reject</button>
                </div>
              </li>
            ))}
          </ul>
        </section>
      )}
      {disputed.length > 0 && (
        <section>
          <h2>Conflicting memories</h2>
          <p className="muted">These disagree and RUFUR couldn’t tell which is right. Agents see both, marked as disputed.</p>
          <ul>
            {disputed.map((m) => (
              <li key={m.id}>
                <button className="link mem-line" onClick={() => onSelect(m.id)}>
                  {m.text}
                </button>
                <p className="muted small">
                  {SOURCE_LABEL[m.source_type] ?? m.source_type}, {since(m.created_at)}
                </p>
                <div className="row">
                  <button className="primary" onClick={() => decide(m.id, "confirm")}>
                    Keep this version
                  </button>
                </div>
              </li>
            ))}
          </ul>
        </section>
      )}
    </div>
  );
}

export function Tasks({ onSelect, version }: { onSelect: Select; version: number }) {
  const [tasks, setTasks] = useState<Task[]>([]);
  const [pools, setPools] = useState<any[]>([]);
  useEffect(() => {
    api.tasks().then(setTasks);
    api.scratchPools().then(setPools);
  }, [version]);
  if (!tasks.length && !pools.length)
    return <Empty text="No handovers or subagents yet. Agents leave handovers here, and subagent memory pools appear while they run." />;
  return (
    <div className="pane tasks">
      {pools.length > 0 && (
        <section>
          <h2>Subagent memory</h2>
          <p className="muted">Each subagent gets a private pool seeded with only what it needs. When it closes, useful findings go back to short-term memory.</p>
          <table className="pools">
            <thead>
              <tr>
                <th scope="col">Purpose</th>
                <th scope="col">Started by</th>
                <th scope="col">Seeded</th>
                <th scope="col">Written</th>
                <th scope="col">Status</th>
              </tr>
            </thead>
            <tbody>
              {pools.map((p) => (
                <tr key={p.id}>
                  <td>{p.purpose}</td>
                  <td>{p.owner_agent}</td>
                  <td>{p.members}</td>
                  <td>{p.own_memories}</td>
                  <td>{p.closed_at ? `closed ${since(p.closed_at)}` : `open, expires ${until(p.expires_at)}`}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </section>
      )}
      {tasks.map((t) => (
        <article key={t.id} className="task">
          <header>
            <h2>{t.title || t.packet.goal}</h2>
            <p className="handoff">
              <span>{t.from_agent ?? "unknown"}</span>
              <span className={`wire ${t.to_agent ? "done" : ""}`} aria-hidden />
              <span>{t.to_agent ?? "waiting for an agent"}</span>
            </p>
          </header>
          <div className="cols">
            <div>
              <h3>Done</h3>
              <ul>{t.packet.steps_done.map((s) => <li key={s}>{s}</li>)}</ul>
            </div>
            <div>
              <h3>Next</h3>
              <ul>{t.packet.steps_pending.map((s) => <li key={s}>{s}</li>)}</ul>
            </div>
          </div>
          {t.packet.decisions.length > 0 && (
            <>
              <h3>Decisions</h3>
              <ul>
                {t.packet.decisions.map((d) => (
                  <li key={d.decision}>
                    {d.decision}
                    {d.reason && <span className="muted">, because {d.reason}</span>}
                  </li>
                ))}
              </ul>
            </>
          )}
          {t.packet.memory_ids.length > 0 && (
            <p className="muted small">
              Carries {t.packet.memory_ids.length} memories with sources:{" "}
              {t.packet.memory_ids.map((id, i) => (
                <button key={id} className="link" onClick={() => onSelect(id)}>
                  {i + 1}
                </button>
              ))}
            </p>
          )}
        </article>
      ))}
    </div>
  );
}

export function Metrics({ version, onChanged }: { version: number; onChanged: () => void }) {
  const [m, setM] = useState<any>(null);
  const [audit, setAudit] = useState<Awaited<ReturnType<typeof api.audit>>>([]);
  const [runs, setRuns] = useState<Awaited<ReturnType<typeof api.runs>>>([]);
  const [stm, setStm] = useState<any>(null);
  const [busy, setBusy] = useState(false);
  useEffect(() => {
    api.metrics().then(setM);
    api.audit().then(setAudit);
    api.runs().then(setRuns);
    api.stmStats().then(setStm);
  }, [version]);

  async function consolidateNow() {
    setBusy(true);
    try {
      await api.consolidate();
      onChanged();
    } finally {
      setBusy(false);
    }
  }
  if (!m) return null;
  const ops: [string, string][] = [
    ["recall", "Answering a question"],
    ["extract", "Learning from a conversation"],
    ["remember", "Saving a memory directly"],
    ["forget", "Forgetting and verifying"],
  ];
  return (
    <div className="pane metrics">
      <section className="headline">
        <p>
          An agent receives about <strong>{Math.round(m.avg_recall_tokens)}</strong> tokens of memory per question, instead of the full{" "}
          <strong>{m.full_history_tokens.toLocaleString()}</strong>-token history
          {m.token_savings_pct !== null && <> ({m.token_savings_pct}% less)</>}.
        </p>
        <p className="muted">
          {Object.entries(m.counts as Record<string, number>)
            .map(([k, v]) => `${v} ${k}`)
            .join(", ")}
          ; {m.abstentions} questions answered with “I don’t know”; {m.tombstones} forgotten items blocked from being relearned.
        </p>
      </section>
      <table>
        <thead>
          <tr>
            <th scope="col">Operation</th>
            <th scope="col">Count</th>
            <th scope="col">Typical time</th>
            <th scope="col">Slowest 5%</th>
            <th scope="col">Model tokens</th>
          </tr>
        </thead>
        <tbody>
          {ops.map(([k, label]) => (
            <tr key={k}>
              <th scope="row">{label}</th>
              <td>{m.ops[k].count}</td>
              <td>{fmtMs(m.ops[k].p50_ms)}</td>
              <td>{fmtMs(m.ops[k].p95_ms)}</td>
              <td>{m.ops[k].llm_tokens.toLocaleString()}</td>
            </tr>
          ))}
        </tbody>
      </table>
      <section>
        <h2>Short-term memory</h2>
        {stm && (
          <p className="muted">
            {stm.unconsolidated} {stm.unconsolidated === 1 ? "memory is" : "memories are"} waiting to be filed.{" "}
            {stm.suggested_h
              ? `Short-term items are typically reused within ${stm.p50_h} h (95% within ${stm.p95_h} h); a ${stm.suggested_h} h lifetime fits that (currently ${stm.current_h} h).`
              : `Short-term memories live ${stm.current_h} h. After ${20 - (stm.samples ?? 0)} more reuses this will suggest a lifetime based on how long they're actually needed.`}
          </p>
        )}
        <div className="row">
          <button className="primary" onClick={consolidateNow} disabled={busy}>
            {busy ? "Filing…" : "Consolidate now"}
          </button>
        </div>
        {runs.length > 0 && (
          <table className="runs">
            <thead>
              <tr>
                <th scope="col">When</th>
                <th scope="col">Trigger</th>
                <th scope="col">Filed</th>
                <th scope="col">Kept short-term</th>
                <th scope="col">Merged</th>
                <th scope="col">Where they went</th>
              </tr>
            </thead>
            <tbody>
              {runs.map((r) => (
                <tr key={r.id}>
                  <td>{since(r.started_at)}</td>
                  <td>{r.trigger}</td>
                  <td>{r.stats.promoted ?? 0}</td>
                  <td>{r.stats.discarded ?? 0}</td>
                  <td>{r.stats.merged ?? 0}</td>
                  <td>
                    {Object.entries((r.stats.by_store ?? {}) as Record<string, number>)
                      .map(([k, v]) => `${v} to ${STORE_LABEL[k] ?? k}`)
                      .join(", ") || "–"}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </section>
      <section>
        <h2>Recent activity</h2>
        <ol className="audit">
          {audit.map((a) => (
            <li key={a.id}>
              <time>{since(a.created_at)}</time> <strong>{a.agent}</strong> {a.action.replace(".", " ")}
            </li>
          ))}
        </ol>
      </section>
    </div>
  );
}

function fmtMs(ms: number) {
  if (!ms) return "–";
  return ms < 1000 ? `${Math.round(ms)} ms` : `${(ms / 1000).toFixed(1)} s`;
}

function Empty({ text }: { text: string }) {
  return (
    <div className="pane empty">
      <p>{text}</p>
    </div>
  );
}
