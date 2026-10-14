import cytoscape, { type Core, type ElementDefinition } from "cytoscape";
import { Maximize2, Minus, Plus } from "lucide-react";
import { type ReactNode, useEffect, useRef } from "react";

import { usePrefs, useReducedEffects } from "@/hooks/usePrefs";
import type { GraphModel, GraphMode } from "@/utils/graph";

function cssVar(name: string): string {
  return getComputedStyle(document.documentElement).getPropertyValue(name).trim() || "#888";
}

/** Mix two hex colours (Cytoscape does not understand CSS color-mix()). */
export function mixHex(a: string, b: string, t: number): string {
  const parse = (h: string) => {
    const x = h.replace("#", "");
    const full = x.length === 3 ? x.split("").map((ch) => ch + ch).join("") : x.slice(0, 6);
    const n = parseInt(full, 16);
    return Number.isNaN(n) ? [128, 128, 128] : [(n >> 16) & 255, (n >> 8) & 255, n & 255];
  };
  const pa = parse(a);
  const pb = parse(b);
  const m = pa.map((v, i) => Math.round(v * t + pb[i] * (1 - t)));
  return `#${m.map((v) => v.toString(16).padStart(2, "0")).join("")}`;
}

function buildStyle(): cytoscape.StylesheetJson {
  const c = {
    fg: cssVar("--fg"),
    fg2: cssVar("--fg-2"),
    fg3: cssVar("--fg-3"),
    surface: cssVar("--surface-2"),
    line: cssVar("--fg-3"),
    cyan: cssVar("--cyan"),
    violet: cssVar("--violet"),
    crit: cssVar("--crit"),
    warn: cssVar("--warn"),
    info: cssVar("--info"),
    bg: cssVar("--bg"),
  };
  return [
    {
      selector: "node",
      style: {
        "background-color": c.surface,
        "border-width": 2,
        "border-color": c.line,
        label: "data(label)",
        color: c.fg,
        "font-family": "JetBrains Mono Variable, ui-monospace, monospace",
        "font-size": 10,
        "text-valign": "bottom",
        "text-margin-y": 7,
        "text-wrap": "ellipsis",
        "text-max-width": "120px",
        "text-background-color": c.bg,
        "text-background-opacity": 0.75,
        "text-background-padding": "2px",
        "text-background-shape": "roundrectangle",
        width: 34,
        height: 34,
        "transition-property": "border-color, background-color, width, height",
        "transition-duration": 250,
      },
    },
    { selector: "node.thread", style: { shape: "ellipse", "border-color": c.cyan, "background-color": mixHex(c.cyan, c.surface, 0.14) } },
    { selector: "node.thread.blocked_lock", style: { "border-color": c.warn, "background-color": mixHex(c.warn, c.surface, 0.14) } },
    { selector: "node.thread.blocked_join", style: { "border-color": c.info } },
    { selector: "node.thread.waiting_cond", style: { "border-color": c.violet, "border-style": "dashed" } },
    { selector: "node.thread.exited", style: { opacity: 0.45, "border-style": "dotted" } },
    {
      selector: "node.lock",
      style: { shape: "round-rectangle", width: 46, height: 24, "border-color": c.violet, "background-color": mixHex(c.violet, c.surface, 0.12) },
    },
    { selector: "node.lock.held", style: { "background-color": mixHex(c.violet, c.surface, 0.28) } },
    {
      selector: "node.cycle",
      style: { "border-color": c.crit, "border-width": 3, "underlay-color": c.crit, "underlay-opacity": 0.22, "underlay-padding": 7, "underlay-shape": "ellipse" },
    },
    { selector: "node.potential", style: { "border-color": c.warn, "border-width": 3, "underlay-color": c.warn, "underlay-opacity": 0.18, "underlay-padding": 6 } },
    { selector: "node.potential-filtered", style: { "border-color": c.fg3, "border-style": "dashed" } },
    {
      selector: "edge",
      style: {
        width: 1.8,
        "curve-style": "bezier",
        "target-arrow-shape": "triangle",
        "arrow-scale": 0.95,
        "line-color": c.line,
        "target-arrow-color": c.line,
        label: "data(label)",
        "font-size": 9,
        color: c.fg3,
        "font-family": "JetBrains Mono Variable, ui-monospace, monospace",
        "text-rotation": "autorotate",
        "text-margin-y": -8,
        "text-background-color": c.bg,
        "text-background-opacity": 0.7,
        "text-background-padding": "1px",
      },
    },
    { selector: "edge.holds", style: { "line-color": c.violet, "target-arrow-color": c.violet } },
    { selector: "edge.waits", style: { "line-style": "dashed", "line-dash-pattern": [6, 4], "line-color": c.warn, "target-arrow-color": c.warn } },
    { selector: "edge.join", style: { "line-style": "dotted", "line-color": c.info, "target-arrow-color": c.info } },
    { selector: "edge.reacquire", style: { "line-style": "dashed", "line-dash-pattern": [2, 4], "line-color": c.violet, "target-arrow-color": c.violet } },
    { selector: "edge.order", style: { "line-color": c.fg3, "target-arrow-color": c.fg3, width: 1.6 } },
    { selector: "edge.timed", style: { opacity: 0.75 } },
    { selector: "edge.cycle", style: { "line-color": c.crit, "target-arrow-color": c.crit, width: 3, color: c.crit, "z-index": 10 } },
    { selector: "edge.potential", style: { "line-color": c.warn, "target-arrow-color": c.warn, width: 2.6, "z-index": 9 } },
    { selector: "node:selected", style: { "overlay-color": c.cyan, "overlay-opacity": 0.14, "overlay-padding": 6, "border-color": c.cyan } },
    { selector: "edge:selected", style: { "overlay-color": c.cyan, "overlay-opacity": 0.1 } },
    { selector: ".faded", style: { opacity: 0.18 } },
  ] as cytoscape.StylesheetJson;
}

function toElements(model: GraphModel): ElementDefinition[] {
  return [
    ...model.nodes.map((n) => ({ group: "nodes" as const, data: { id: n.id, label: n.label, kind: n.kind, ref: n.ref, order: n.order ?? 1000 }, classes: n.classes.join(" ") })),
    ...model.edges.map((e) => ({ group: "edges" as const, data: { id: e.id, source: e.source, target: e.target, label: e.label ?? "" }, classes: e.classes.join(" ") })),
  ];
}

export function Graph2D({
  model,
  mode,
  onSelect,
  selected,
  height = 520,
  compact = false,
  focus,
}: {
  model: GraphModel;
  mode: GraphMode;
  onSelect?: (id: string | null) => void;
  selected?: string | null;
  height?: number | string;
  compact?: boolean;
  focus?: string[];
}) {
  const host = useRef<HTMLDivElement>(null);
  const cy = useRef<Core | null>(null);
  const reduced = useReducedEffects();
  const theme = usePrefs((p) => p.theme);
  const onSelectRef = useRef(onSelect);
  onSelectRef.current = onSelect;

  // create once
  useEffect(() => {
    if (!host.current) return;
    const inst = cytoscape({
      container: host.current,
      style: buildStyle(),
      minZoom: 0.2,
      maxZoom: 3,
      boxSelectionEnabled: false,
      autoungrabify: compact,
      userPanningEnabled: !compact,
      userZoomingEnabled: !compact,
    });
    inst.on("tap", "node", (e) => onSelectRef.current?.(e.target.id()));
    inst.on("tap", (e) => {
      if (e.target === inst) onSelectRef.current?.(null);
    });
    cy.current = inst;
    // keep the canvas in sync with the container (page transitions, sidebar collapse, window resize)
    let frame = 0;
    const ro = new ResizeObserver(() => {
      cancelAnimationFrame(frame);
      frame = requestAnimationFrame(() => {
        inst.resize();
        if (inst.elements().length) inst.fit(undefined, 40);
      });
    });
    ro.observe(host.current);
    return () => {
      ro.disconnect();
      cancelAnimationFrame(frame);
      inst.destroy();
      cy.current = null;
    };
  }, [compact]);

  // re-style on theme change
  useEffect(() => {
    cy.current?.style(buildStyle());
  }, [theme]);

  // diff elements
  useEffect(() => {
    const inst = cy.current;
    if (!inst) return;
    const els = toElements(model);
    const want = new Set(els.map((e) => e.data.id as string));
    let added = false;
    inst.batch(() => {
      inst.elements().forEach((el) => {
        if (!want.has(el.id())) el.remove();
      });
      for (const e of els) {
        const id = e.data.id as string;
        const ex = inst.getElementById(id);
        if (ex.nonempty()) {
          ex.data(e.data);
          ex.classes(e.classes as string);
        } else {
          inst.add(e);
          added = true;
        }
      }
    });
    if (added || inst.nodes().length <= 1) {
      let layout: cytoscape.LayoutOptions;
      const hasCycle = inst.nodes().some((n) => n.data("order") < 1000);
      if (mode === "waitfor" || hasCycle || inst.nodes().length <= 6) {
        // ring layout computed here: nodes on a cycle are placed consecutively so the
        // cycle reads as a ring (Cytoscape's circle layout ignores `sort` in this version)
        const ns = inst.nodes().sort((a, b) => a.data("order") - b.data("order"));
        const r = Math.max(110, ns.length * 46);
        const pos: Record<string, cytoscape.Position> = {};
        ns.forEach((node, i) => {
          const a = -Math.PI / 2 + (2 * Math.PI * i) / ns.length;
          pos[node.id()] = { x: r * Math.cos(a), y: r * Math.sin(a) };
        });
        layout = { name: "preset", positions: (node: cytoscape.NodeSingular) => pos[node.id()], animate: !reduced, animationDuration: 450, fit: true, padding: 40 } as cytoscape.LayoutOptions;
      } else {
        layout = { name: "cose", animate: !reduced, animationDuration: 600, padding: 40, nodeRepulsion: () => 9000, idealEdgeLength: () => 90, randomize: false, fit: true } as cytoscape.LayoutOptions;
      }
      inst.resize();
      const l = inst.layout(layout);
      l.one("layoutstop", () => inst.fit(undefined, 40));
      l.run();
    }
  }, [model, mode, reduced]);

  // selection highlight
  useEffect(() => {
    const inst = cy.current;
    if (!inst) return;
    inst.elements().unselect();
    if (selected) inst.getElementById(selected).select();
  }, [selected, model]);

  // focus a subset (e.g. a finding's threads/locks)
  useEffect(() => {
    const inst = cy.current;
    if (!inst) return;
    inst.elements().removeClass("faded");
    if (focus && focus.length) {
      let keep = inst.collection();
      for (const id of focus) {
        const el = inst.getElementById(id);
        if (el.nonempty()) keep = keep.union(el);
      }
      const nb = keep.union(keep.edgesWith(keep));
      inst.elements().difference(nb).addClass("faded");
    }
  }, [focus, model]);

  // animated "marching ants" on cycle edges (skipped with reduced effects)
  useEffect(() => {
    if (reduced) return;
    let raf = 0;
    let offset = 0;
    const tick = () => {
      const inst = cy.current;
      if (inst) {
        offset = (offset + 0.6) % 1000;
        inst.edges(".cycle, .potential, .waits").style("line-dash-offset", -offset);
      }
      raf = requestAnimationFrame(tick);
    };
    raf = requestAnimationFrame(tick);
    return () => cancelAnimationFrame(raf);
  }, [reduced]);

  const zoom = (f: number) => {
    const inst = cy.current;
    if (!inst) return;
    inst.zoom({ level: inst.zoom() * f, renderedPosition: { x: inst.width() / 2, y: inst.height() / 2 } });
  };

  return (
    <div className="relative" style={{ height }}>
      {/* Cytoscape forces position:relative on its container, so size it explicitly */}
      <div ref={host} className="cy-host" style={{ width: "100%", height: "100%" }} role="img" aria-label="Concurrency graph" />
      {model.nodes.length === 0 && (
        <div className="pointer-events-none absolute inset-0 grid place-items-center text-sm text-fg-3">No nodes to show in this view yet.</div>
      )}
      {!compact && (
        <div className="absolute right-3 top-3 flex flex-col gap-1">
          <button className="btn p-1.5" onClick={() => zoom(1.25)} aria-label="Zoom in" title="Zoom in">
            <Plus size={14} />
          </button>
          <button className="btn p-1.5" onClick={() => zoom(0.8)} aria-label="Zoom out" title="Zoom out">
            <Minus size={14} />
          </button>
          <button className="btn p-1.5" onClick={() => cy.current?.animate({ fit: { eles: cy.current.elements(), padding: 40 } }, { duration: reduced ? 0 : 350 })} aria-label="Fit to screen" title="Fit to screen">
            <Maximize2 size={14} />
          </button>
        </div>
      )}
    </div>
  );
}

export function GraphLegend({ mode }: { mode: GraphMode }) {
  const item = (swatch: ReactNode, label: string) => (
    <span className="inline-flex items-center gap-1.5 text-xs text-fg-2">
      {swatch}
      {label}
    </span>
  );
  const line = (color: string, dash?: string) => (
    <svg width="26" height="8" aria-hidden>
      <line x1="1" y1="4" x2="25" y2="4" stroke={color} strokeWidth="2" strokeDasharray={dash} />
    </svg>
  );
  return (
    <div className="flex flex-wrap items-center gap-x-4 gap-y-1.5">
      {mode !== "lockorder" && item(<span className="h-3 w-3 rounded-full border-2" style={{ borderColor: "var(--cyan)" }} />, "thread")}
      {mode !== "waitfor" && item(<span className="h-2.5 w-4 rounded-[3px] border-2" style={{ borderColor: "var(--violet)" }} />, "lock")}
      {mode === "resource" && item(line("var(--violet)"), "held by")}
      {mode !== "lockorder" && item(line("var(--warn)", "5 3"), "waits for")}
      {mode !== "lockorder" && item(line("var(--info)", "1 3"), "joins")}
      {mode !== "lockorder" && item(line("var(--violet)", "2 3"), "re-acquire (inferred)")}
      {mode === "lockorder" && item(line("var(--fg-3)"), "acquired while holding")}
      {mode !== "lockorder" && item(line("var(--crit)"), "deadlock cycle")}
      {mode === "lockorder" && item(line("var(--warn)"), "potential cycle")}
    </div>
  );
}
