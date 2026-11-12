import { useEffect, useMemo, useRef, useState } from "react";
import { forceX, forceY } from "d3-force";
import ForceGraph2D, { ForceGraphMethods } from "react-force-graph-2d";
import type { GraphLink, GraphNode } from "./api";

const C = {
  print: "#EEF3F9",
  faint: "rgba(238,243,249,0.28)",
  ghost: "rgba(238,243,249,0.18)",
  amber: "#E2A23A",
  rose: "#D94F8C",
  glow: "#9FD3FF",
  label: "rgba(238,243,249,0.62)",
};

interface Props {
  nodes: GraphNode[];
  links: GraphLink[];
  highlight: Set<string>;
  selected: string | null;
  onSelect: (id: string) => void;
  upTo: number | null; // replay: only show memories created before this time
}

function radius(n: GraphNode) {
  return 3 + (n.importance ?? 0.5) * 6;
}

export default function BrainGraph({ nodes, links, highlight, selected, onSelect, upTo }: Props) {
  const wrap = useRef<HTMLDivElement>(null);
  const fg = useRef<ForceGraphMethods<any, any>>();
  const [size, setSize] = useState({ w: 800, h: 600 });
  const fitted = useRef(false);

  useEffect(() => {
    const el = wrap.current;
    if (!el) return;
    const ro = new ResizeObserver(() => setSize({ w: el.clientWidth, h: el.clientHeight }));
    ro.observe(el);
    return () => ro.disconnect();
  }, []);

  // Keep node objects stable across refreshes so the layout doesn't explode on every live update.
  const cache = useRef(new Map<string, GraphNode>());
  const data = useMemo(() => {
    const visible = nodes.filter((n) => n.kind !== "memory" || upTo === null || (n.created_at ?? 0) <= upTo);
    const memIds = new Set(visible.filter((n) => n.kind === "memory").map((n) => n.id));
    const ns = visible.map((n) => {
      const prev = cache.current.get(n.id);
      const merged = prev ? Object.assign(prev, n, { x: prev.x, y: prev.y }) : { ...n };
      cache.current.set(n.id, merged);
      return merged;
    });
    const ids = new Set(ns.map((n) => n.id));
    const ls = links
      .map((l) => ({ ...l, source: typeof l.source === "string" ? l.source : l.source.id, target: typeof l.target === "string" ? l.target : l.target.id }))
      .filter((l) => ids.has(l.source as string) && ids.has(l.target as string))
      .filter((l) => l.kind !== "about" || memIds.has(l.source as string));
    // Drop subjects with no visible memory attached (e.g. during replay).
    const used = new Set(ls.flatMap((l) => [l.source as string, l.target as string]));
    return { nodes: ns.filter((n) => n.kind === "memory" || used.has(n.id)), links: ls };
  }, [nodes, links, upTo]);

  useEffect(() => {
    if (highlight.size && fg.current) {
      const pts = data.nodes.filter((n) => highlight.has(n.id) && n.x !== undefined);
      if (pts.length) {
        const x = pts.reduce((s, n) => s + (n.x ?? 0), 0) / pts.length;
        const y = pts.reduce((s, n) => s + (n.y ?? 0), 0) / pts.length;
        // The answer panel covers the left ~30rem; shift the view so highlighted nodes sit to its right.
        const k = fg.current.zoom();
        fg.current.centerAt(x - 240 / k, y, 600);
      }
    }
  }, [highlight, data.nodes]);

  // Gentle pull to the centre so unrelated clusters stay on screen instead of drifting apart.
  useEffect(() => {
    const g = fg.current;
    if (!g) return;
    g.d3Force("x", forceX(0).strength(0.06) as any);
    g.d3Force("y", forceY(0).strength(0.06) as any);
    g.d3Force("charge")?.strength(-60);
    g.d3ReheatSimulation();
  }, [data]);

  const showLabels = data.nodes.length < 60;
  const reduceMotion = typeof window !== "undefined" && window.matchMedia("(prefers-reduced-motion: reduce)").matches;

  return (
    <div ref={wrap} className="graph" role="img" aria-label="Memory graph">
      <ForceGraph2D
        ref={fg}
        width={size.w}
        height={size.h}
        graphData={data}
        backgroundColor="rgba(0,0,0,0)"
        nodeId="id"
        cooldownTicks={120}
        onEngineStop={() => {
          if (!fitted.current && fg.current) {
            fitted.current = true;
            fg.current.zoomToFit(500, 90);
          }
        }}
        d3VelocityDecay={0.3}
        autoPauseRedraw={reduceMotion}
        linkColor={(l: any) => (l.kind === "supersedes" ? C.faint : l.kind === "relation" ? C.label : l.kind === "co_source" ? "rgba(238,243,249,0.08)" : "rgba(238,243,249,0.14)")}
        linkWidth={(l: any) => (l.kind === "relation" ? 1.2 : 0.6)}
        linkLineDash={(l: any) => (l.kind === "supersedes" ? [3, 3] : null)}
        linkDirectionalArrowLength={(l: any) => (l.kind === "supersedes" || l.kind === "relation" ? 4 : 0)}
        linkDirectionalArrowRelPos={1}
        onNodeClick={(n: any) => n.kind === "memory" && onSelect(n.id)}
        nodeLabel={(n: any) => n.label}
        nodePointerAreaPaint={(n: any, color, ctx) => {
          ctx.fillStyle = color;
          ctx.beginPath();
          ctx.arc(n.x, n.y, n.kind === "memory" ? radius(n) + 2 : 4, 0, 2 * Math.PI);
          ctx.fill();
        }}
        nodeCanvasObject={(n: any, ctx, scale) => {
          if (n.kind !== "memory") {
            ctx.fillStyle = C.label;
            ctx.fillRect(n.x - 1.5, n.y - 1.5, 3, 3);
            const fs = Math.max(10 / scale, 2.5);
            ctx.font = `500 ${fs}px "IBM Plex Sans Condensed", sans-serif`;
            ctx.textAlign = "center";
            ctx.fillText(n.label, n.x, n.y - 4);
            return;
          }
          const r = radius(n);
          const t = performance.now() / 1000;
          if (highlight.has(n.id)) {
            ctx.beginPath();
            ctx.arc(n.x, n.y, r + 5, 0, 2 * Math.PI);
            ctx.fillStyle = "rgba(159,211,255,0.22)";
            ctx.fill();
            ctx.strokeStyle = C.glow;
            ctx.lineWidth = 1.2;
            ctx.stroke();
          }
          ctx.beginPath();
          ctx.arc(n.x, n.y, r, 0, 2 * Math.PI);
          ctx.setLineDash([]);
          switch (n.status) {
            case "active":
              if (n.tier === "stm") {
                // Short-term: a ring with a small core, so it reads as "not filed yet".
                ctx.strokeStyle = C.print;
                ctx.lineWidth = 1.4;
                ctx.stroke();
                ctx.beginPath();
                ctx.arc(n.x, n.y, r * 0.45, 0, 2 * Math.PI);
                ctx.fillStyle = C.print;
                ctx.fill();
              } else {
                ctx.fillStyle = C.print;
                ctx.fill();
              }
              break;
            case "disputed":
              ctx.fillStyle = C.rose;
              ctx.fill();
              ctx.beginPath();
              ctx.arc(n.x, n.y, r + 2 + (reduceMotion ? 1 : (Math.sin(t * 3) + 1) * 1.5), 0, 2 * Math.PI);
              ctx.strokeStyle = C.rose;
              ctx.lineWidth = 0.8;
              ctx.stroke();
              break;
            case "quarantined":
              ctx.setLineDash([2, 1.5]);
              ctx.strokeStyle = C.amber;
              ctx.lineWidth = 1.4;
              ctx.stroke();
              ctx.setLineDash([]);
              break;
            default: // superseded, archived, rejected
              ctx.strokeStyle = C.ghost;
              ctx.lineWidth = 0.8;
              ctx.stroke();
          }
          if (selected === n.id) {
            ctx.beginPath();
            ctx.arc(n.x, n.y, r + 3, 0, 2 * Math.PI);
            ctx.strokeStyle = C.print;
            ctx.lineWidth = 1.5;
            ctx.stroke();
          }
          if (showLabels || scale > 1.3 || highlight.has(n.id) || selected === n.id) {
            ctx.font = `400 ${Math.max(11 / scale, 3)}px "Source Serif 4", serif`;
            ctx.fillStyle = n.status === "active" ? C.print : C.label;
            ctx.textAlign = "left";
            const text = n.label.length > 48 ? n.label.slice(0, 47) + "…" : n.label;
            ctx.fillText(text, n.x + r + 3, n.y + 3 / scale);
          }
        }}
      />
    </div>
  );
}
