import { useEffect, useState } from "react";
import { api, ForgetResult, Memory, since, SOURCE_LABEL, STORE_LABEL as FILED_IN, until } from "./api";

const STORE_LABEL: Record<string, string> = {
  direct_lookup: "Lookup by id",
  ledger: "Memory ledger",
  fts: "Keyword index",
  file_chunks: "Document chunks",
  graph: "Relation graph",
  typed_rows: "Tables",
  events: "Raw transcripts",
  recall_probes: "Paraphrased questions",
  byte_scan: "Raw bytes on disk",
};

const STATUS_TEXT: Record<string, string> = {
  active: "In use",
  disputed: "Disputed: conflicts with another memory",
  quarantined: "Held for review: not used in answers",
  superseded: "Replaced by a newer version",
  archived: "Archived: kept but not used",
  rejected: "Rejected",
};

interface Props {
  id: string;
  onClose: () => void;
  onChanged: () => void;
  onSelect: (id: string) => void;
}

export default function Inspector({ id, onClose, onChanged, onSelect }: Props) {
  const [m, setM] = useState<Memory | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [forgetting, setForgetting] = useState(false);
  const [confirmForget, setConfirmForget] = useState(false);
  const [report, setReport] = useState<ForgetResult | null>(null);

  useEffect(() => {
    setM(null);
    setError(null);
    setReport(null);
    setConfirmForget(false);
    api.memory(id).then(setM, (e) => setError(String(e.message)));
  }, [id]);

  async function decide(action: "confirm" | "reject") {
    await api.confirm(id, action);
    setM(await api.memory(id));
    onChanged();
  }

  async function forget() {
    setForgetting(true);
    try {
      setReport(await api.forget(id));
      onChanged();
    } catch (e: any) {
      setError(e.message);
    } finally {
      setForgetting(false);
    }
  }

  const otherSameKey = m ? m.related.filter((r) => !m.history.some((h) => h.id === r.id)) : [];

  return (
    <aside className="inspector" aria-label="Memory details">
      <button className="close" onClick={onClose} aria-label="Close details">
        ×
      </button>
      {error && !report && <p className="error">This memory is no longer available. {error}</p>}
      {report && <ForgetReport r={report} />}
      {!report && m && (
        <>
          <p className={`status s-${m.status}`}>
            {STATUS_TEXT[m.status] ?? m.status}
            <span className={`tier tier-${m.tier}`}>{m.tier === "stm" ? "Short-term" : "Long-term"}</span>
          </p>
          <h2 className="mem-text">{m.text}</h2>
          {m.scope && <p className="scope">Applies to: {m.scope}</p>}
          <p className="lifecycle muted small">
            {m.tier === "stm"
              ? m.store === "discard"
                ? `Kept short-term only; expires ${m.expires_at ? until(m.expires_at) : "soon"}.`
                : `Waiting to be filed into long-term memory; expires ${m.expires_at ? until(m.expires_at) : "soon"} unless filed or used.`
              : `Filed in ${FILED_IN[m.store] ?? m.store}.`}
            {m.revalidate_at && m.revalidate_at < Date.now() / 1000 && m.status === "active" && " Old enough that it may have changed: confirm before relying on it."}
            {m.corroboration > 1 && ` Confirmed by ${m.corroboration} independent sources.`}
          </p>

          <dl className="facts">
            <div>
              <dt>Kind</dt>
              <dd>{m.type}</dd>
            </div>
            <div>
              <dt>Trust</dt>
              <dd>{m.trust.replace("_", " ")}</dd>
            </div>
            <div>
              <dt>Sensitivity</dt>
              <dd>{m.sensitivity}</dd>
            </div>
            <div>
              <dt>Written by</dt>
              <dd>{m.author_agent}</dd>
            </div>
            <div>
              <dt>Pool</dt>
              <dd>{m.pool}</dd>
            </div>
            <div>
              <dt>Used</dt>
              <dd>{m.access_count} times</dd>
            </div>
          </dl>

          {(m.status === "quarantined" || m.status === "disputed") && (
            <div className="decide">
              <p>{m.status === "disputed" ? "Is this the correct version?" : "Should agents be allowed to use this?"}</p>
              <button className="primary" onClick={() => decide("confirm")}>
                {m.status === "disputed" ? "Keep this version" : "Approve"}
              </button>
              <button onClick={() => decide("reject")}>Reject</button>
            </div>
          )}

          <section>
            <h3>Where it came from</h3>
            {m.sources.length === 0 && <p className="muted">No source recorded.</p>}
            {m.sources.map((s) => (
              <figure key={s.event_id} className="source">
                <blockquote>{s.quote}</blockquote>
                <figcaption>
                  {SOURCE_LABEL[s.source_type] ?? s.source_type}, via {s.agent}, {s.created_at ? since(s.created_at) : ""}
                </figcaption>
              </figure>
            ))}
          </section>

          {m.flags.length > 0 && (
            <section>
              <h3>Warnings</h3>
              <ul className="flags">
                {m.flags.map((f) => (
                  <li key={f}>{flagText(f)}</li>
                ))}
              </ul>
            </section>
          )}

          {m.history.length > 1 && (
            <section>
              <h3>Versions</h3>
              <ol className="history">
                {m.history.map((h) => (
                  <li key={h.id} className={h.id === m.id ? "current" : ""}>
                    <button className="link" onClick={() => onSelect(h.id)} disabled={h.id === m.id}>
                      {h.text}
                    </button>
                    <span className="muted">
                      {since(h.valid_from)}
                      {h.valid_to ? `, replaced ${since(h.valid_to)}` : ""}
                    </span>
                  </li>
                ))}
              </ol>
            </section>
          )}

          {otherSameKey.length > 0 && (
            <section>
              <h3>Other memories with the same key</h3>
              <ul className="related">
                {otherSameKey.map((r) => (
                  <li key={r.id}>
                    <button className="link" onClick={() => onSelect(r.id)}>
                      {r.text}
                    </button>
                    <span className={`chip s-${r.status}`}>{r.status}</span>
                  </li>
                ))}
              </ul>
            </section>
          )}

          <section>
            <h3>Stored in</h3>
            <p className="muted">
              {summarizeLineage(m.lineage)}. Forgetting removes every one of these and checks that nothing is left.
            </p>
          </section>

          <div className="forget">
            {!confirmForget ? (
              <button className="danger" onClick={() => setConfirmForget(true)}>
                Forget this memory
              </button>
            ) : (
              <>
                <p>This deletes the memory, its earlier versions and everything derived from it, then verifies the deletion. It can’t be undone.</p>
                <button className="danger" onClick={forget} disabled={forgetting}>
                  {forgetting ? "Forgetting and verifying…" : "Forget permanently"}
                </button>
                <button onClick={() => setConfirmForget(false)} disabled={forgetting}>
                  Keep it
                </button>
              </>
            )}
          </div>
        </>
      )}
    </aside>
  );
}

function flagText(f: string): string {
  if (f === "instruction_like") return "Contains text that tries to give instructions. Treated as data only.";
  if (f === "unverified_user_claim") return "An agent said the user stated this, but no matching user message was found.";
  if (f === "user_confirmed") return "Confirmed by the user.";
  if (f === "agent_quarantined") return "Written by an agent that has been quarantined.";
  if (f.startsWith("supersede_needs_confirmation")) return "Would replace a trusted memory. Needs your approval.";
  return f;
}

function summarizeLineage(items: { child_kind: string }[]): string {
  const names: Record<string, string> = {
    fts: "keyword index",
    vector: "semantic index",
    edge: "relation graph",
    file: "document file",
    file_chunk: "document chunks",
    typed_row: "table",
  };
  const kinds = new Set(items.map((i) => i.child_kind));
  const parts = ["ledger", ...[...kinds].map((k) => names[k] ?? k)];
  return parts.join(", ").replace(/^./, (c) => c.toUpperCase());
}

function ForgetReport({ r }: { r: ForgetResult }) {
  if (!r.report) return <p>Forgotten {r.forgotten.length} memories.</p>;
  const pct = Math.round(r.report.completeness * 100);
  return (
    <div className="report">
      <p className="status s-rejected">Forgotten</p>
      <h2 className="mem-text">{pct}% of deletion checks passed</h2>
      <p className="muted">
        Removed {r.forgotten.length} {r.forgotten.length === 1 ? "memory" : "memories"} and redacted {r.redacted_events} transcript{" "}
        {r.redacted_events === 1 ? "entry" : "entries"}. A tombstone stops it from being learned again.
      </p>
      <ul className="checks">
        {Object.entries(r.report.stores).map(([k, v]) => (
          <li key={k} className={v.passed ? "pass" : "fail"}>
            <span aria-hidden>{v.passed ? "✓" : "✗"}</span> {STORE_LABEL[k] ?? k}
          </li>
        ))}
      </ul>
      <p className="muted small">
        {r.report.probe_queries.length} questions were asked to try to recover it. {r.report.note}
      </p>
    </div>
  );
}
