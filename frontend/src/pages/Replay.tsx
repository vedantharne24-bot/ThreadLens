import { AnimatePresence, motion } from "motion/react";
import { ArrowDownRight, ArrowUpRight, ChevronLeft, ChevronRight, ChevronsLeft, ChevronsRight, GitCompare, History, Info, Layers, Pause, Play } from "lucide-react";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { useSearchParams } from "react-router-dom";

import { Card, CardHeader, EmptyState, PageHeader, Segmented, SeverityBadge, Skeleton, StateBadge, VerdictPill, cx } from "@/components/ui";
import { usePrefs } from "@/hooks/usePrefs";
import { api } from "@/services/api";
import { sessionStore } from "@/services/session";
import { useStore } from "@/services/store";
import type { Comparison, MergedLockOrder, SessionInfo, Snapshot } from "@/types/api";
import { fmtRel, timeAgo, typeColor } from "@/utils/format";
import { type GraphModel, buildGraph } from "@/utils/graph";
import { Graph2D, GraphLegend } from "@/visualization/Graph2D";

type Tab = "replay" | "compare" | "crossrun";

export default function Replay() {
  const [params, setParams] = useSearchParams();
  const tab = (params.get("tab") as Tab) || "replay";
  return (
    <div className="space-y-6">
      <PageHeader
        eyebrow="Replay & compare"
        title={tab === "replay" ? "Step through recorded events" : tab === "compare" ? "Original vs. corrected execution" : "Lock-order analysis across runs"}
        description={
          tab === "replay"
            ? "Event replay re-applies the recorded events to the same deterministic analysis engine to reconstruct what ThreadLens knew at each sequence number. The program is NOT re-executed (that would be deterministic execution replay, as done by rr)."
            : tab === "compare"
              ? "Compare two finished sessions — typically the original program and the corrected one — using measured, recorded results."
              : "Merge lock-order graphs of several executions using stable lock names (ASLR changes addresses between runs) to predict inversions no single run contains."
        }
        actions={
          <Segmented
            value={tab}
            onChange={(t) => setParams({ tab: t })}
            options={[
              { value: "replay", label: <><History size={13} /> Replay</> },
              { value: "compare", label: <><GitCompare size={13} /> Compare</> },
              { value: "crossrun", label: <><Layers size={13} /> Cross-run</> },
            ]}
          />
        }
      />
      <AnimatePresence initial={false}>
        <motion.div key={tab} initial={{ opacity: 0, y: 8 }} animate={{ opacity: 1, y: 0 }} transition={{ duration: 0.2 }}>
          {tab === "replay" && <ReplayTab />}
          {tab === "compare" && <CompareTab />}
          {tab === "crossrun" && <CrossRunTab />}
        </motion.div>
      </AnimatePresence>
    </div>
  );
}

function ReplayTab() {
  const sid = useStore(sessionStore, (s) => s.activeId);
  const liveSeq = useStore(sessionStore, (s) => s.snapshot?.seq ?? -1);
  const findings = useStore(sessionStore, (s) => s.snapshot?.findings ?? []);
  const unit = usePrefs((p) => p.timeUnit);
  const [params, setParams] = useSearchParams();
  const [seq, setSeq] = useState<number>(() => (params.get("seq") ? Number(params.get("seq")) : -2));
  const [snap, setSnap] = useState<Snapshot | null>(null);
  const [lastSeq, setLastSeq] = useState(liveSeq);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [playing, setPlaying] = useState(false);
  const [speed, setSpeed] = useState(4);
  const [mode, setMode] = useState<"resource" | "waitfor">("resource");
  const reqId = useRef(0);

  useEffect(() => setLastSeq((l) => Math.max(l, liveSeq)), [liveSeq]);
  useEffect(() => {
    if (seq === -2 && liveSeq >= 0) setSeq(liveSeq);
  }, [seq, liveSeq]);

  const load = useCallback(
    (s: number) => {
      if (!sid || s < -1) return;
      const id = ++reqId.current;
      setLoading(true);
      api
        .replay(sid, s)
        .then((r) => {
          if (id !== reqId.current) return;
          setSnap(r);
          setError(null);
          if (r.replay) setLastSeq((l) => Math.max(l, r.replay!.last_seq));
        })
        .catch((e) => id === reqId.current && setError(e.message))
        .finally(() => id === reqId.current && setLoading(false));
    },
    [sid],
  );

  useEffect(() => {
    const t = window.setTimeout(() => load(seq), 90);
    return () => window.clearTimeout(t);
  }, [seq, load]);

  useEffect(() => {
    if (!playing) return;
    const timer = window.setInterval(() => {
      setSeq((s) => {
        if (s >= lastSeq) {
          setPlaying(false);
          return s;
        }
        return s + 1;
      });
    }, 1000 / speed);
    return () => window.clearInterval(timer);
  }, [playing, speed, lastSeq]);

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if ((e.target as HTMLElement).closest("input, select, textarea")) return;
      if (e.key === "ArrowRight") setSeq((s) => Math.min(lastSeq, s + 1));
      if (e.key === "ArrowLeft") setSeq((s) => Math.max(0, s - 1));
      if (e.key === " ") {
        e.preventDefault();
        setPlaying((p) => !p);
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [lastSeq]);

  const model = useMemo(() => buildGraph(snap, mode), [snap, mode]);
  if (!sid) return <Card><EmptyState icon={<History size={22} />} title="No session selected" body="Select a session to replay its recorded events." /></Card>;
  const current = snap?.window?.find((e) => e.seq === seq);
  const markers = findings.filter((f) => f.status !== "filtered" && f.severity !== "info");

  return (
    <div className="space-y-5">
      <div className="card flex items-start gap-2.5 px-4 py-3 text-sm text-fg-2">
        <Info size={16} className="mt-0.5 shrink-0 text-info" />
        <span>
          <span className="font-medium text-fg">Event replay, not execution replay.</span> Each position shows the analysis state after applying events 0…{Math.max(seq, 0)} (and the collector heartbeats received at that point).
        </span>
      </div>
      <Card className="p-4">
        <div className="flex flex-wrap items-center gap-2">
          <button className="btn p-2" onClick={() => setSeq(0)} aria-label="First event"><ChevronsLeft size={15} /></button>
          <button className="btn p-2" onClick={() => setSeq((s) => Math.max(0, s - 1))} aria-label="Previous event"><ChevronLeft size={15} /></button>
          <button className="btn btn-primary px-3" onClick={() => setPlaying((p) => !p)} aria-label={playing ? "Pause" : "Play"}>
            {playing ? <Pause size={15} /> : <Play size={15} />} {playing ? "Pause" : "Play"}
          </button>
          <button className="btn p-2" onClick={() => setSeq((s) => Math.min(lastSeq, s + 1))} aria-label="Next event"><ChevronRight size={15} /></button>
          <button className="btn p-2" onClick={() => setSeq(lastSeq)} aria-label="Last event"><ChevronsRight size={15} /></button>
          <select className="input w-auto" value={speed} onChange={(e) => setSpeed(Number(e.target.value))} aria-label="Playback speed">
            {[1, 4, 10, 30, 100].map((s) => (
              <option key={s} value={s}>{s} events/s</option>
            ))}
          </select>
          <div className="ml-auto flex items-center gap-2 text-sm">
            <span className="text-fg-3">seq</span>
            <input
              className="input mono w-28"
              type="number"
              min={0}
              max={lastSeq}
              value={Math.max(seq, 0)}
              onChange={(e) => setSeq(Math.max(0, Math.min(lastSeq, Number(e.target.value) || 0)))}
              aria-label="Sequence number"
            />
            <span className="mono text-fg-3">/ {lastSeq}</span>
          </div>
        </div>
        <div className="relative mt-4">
          <input
            type="range"
            min={0}
            max={Math.max(lastSeq, 0)}
            value={Math.max(seq, 0)}
            onChange={(e) => {
              setPlaying(false);
              setSeq(Number(e.target.value));
              setParams({ tab: "replay", seq: e.target.value }, { replace: true });
            }}
            className="w-full accent-[var(--cyan)]"
            aria-label="Replay position"
          />
          <div className="pointer-events-none relative h-4">
            {markers.map((f) => (
              <button
                key={f.id}
                className="pointer-events-auto absolute top-0 -translate-x-1/2"
                style={{ left: `${(f.detected_seq / Math.max(lastSeq, 1)) * 100}%` }}
                onClick={() => setSeq(f.detected_seq)}
                title={`${f.id}: ${f.title} (seq ${f.detected_seq})`}
              >
                <span className="block h-3 w-3 rotate-45 rounded-[2px]" style={{ background: f.severity === "critical" ? "var(--crit)" : "var(--warn)" }} />
              </button>
            ))}
          </div>
        </div>
        <AnimatePresence mode="wait">
          {current && (
            <motion.div key={current.seq} initial={{ opacity: 0, y: 4 }} animate={{ opacity: 1, y: 0 }} exit={{ opacity: 0 }} className="mt-2 flex flex-wrap items-center gap-3 text-sm">
              <span className="mono text-xs text-fg-3">#{current.seq} · {fmtRel(current.rel_ns, unit)}</span>
              <span className="mono text-xs" style={{ color: typeColor(current.type) }}>{current.type}</span>
              <span className="text-fg">{current.description}</span>
            </motion.div>
          )}
        </AnimatePresence>
      </Card>

      <div className="grid gap-5 xl:grid-cols-[1fr_360px]">
        <Card className="relative overflow-hidden">
          <div className="flex flex-wrap items-center justify-between gap-3 border-b hairline px-4 py-2.5">
            <GraphLegend mode={mode} />
            <Segmented value={mode} onChange={setMode} options={[{ value: "resource", label: "Resources" }, { value: "waitfor", label: "Wait-for" }]} />
          </div>
          {error && <div className="p-4 text-sm text-crit">{error}</div>}
          {!snap && !error ? <Skeleton className="h-[460px] rounded-none" /> : <Graph2D model={model} mode={mode} height={460} />}
          {loading && <div className="absolute right-4 top-14 text-xs text-cyan">reconstructing…</div>}
        </Card>
        <div className="space-y-4">
          <Card>
            <CardHeader eyebrow={`state at #${Math.max(seq, 0)}`} title="Threads" />
            <div className="max-h-56 divide-y divide-[var(--line)] overflow-y-auto">
              {snap?.threads.map((t) => (
                <div key={t.index} className="flex items-center justify-between gap-2 px-4 py-2 text-sm">
                  <span className="mono truncate text-xs"><span className="text-fg-3">T{t.index}</span> {t.label}</span>
                  <StateBadge state={t.state} inCycle={t.in_cycle} />
                </div>
              ))}
            </div>
          </Card>
          <Card>
            <CardHeader eyebrow={`known at #${Math.max(seq, 0)}`} title="Findings" />
            <div className="divide-y divide-[var(--line)]">
              {snap && snap.findings.filter((f) => f.severity !== "info").length === 0 && <div className="px-4 py-6 text-center text-sm text-fg-3">None yet at this point.</div>}
              {snap?.findings
                .filter((f) => f.severity !== "info")
                .map((f) => (
                  <div key={f.id} className="px-4 py-2.5">
                    <div className="flex items-center gap-2">
                      <SeverityBadge severity={f.severity} />
                      <span className="chip">{f.status}</span>
                    </div>
                    <div className="mt-1 text-sm">{f.title}</div>
                  </div>
                ))}
            </div>
          </Card>
          <Card>
            <CardHeader eyebrow="Context" title="Events around this point" />
            <ol className="p-2">
              {snap?.window?.map((e) => (
                <li key={e.seq}>
                  <button onClick={() => setSeq(e.seq)} className={cx("flex w-full gap-2 rounded-md px-2 py-1 text-left text-xs hover:bg-surface-2", e.seq === seq && "bg-surface-3")}>
                    <span className="mono w-10 shrink-0 text-fg-3">#{e.seq}</span>
                    <span className={cx("truncate", e.seq === seq ? "text-fg" : "text-fg-2")}>{e.description}</span>
                  </button>
                </li>
              ))}
            </ol>
          </Card>
        </div>
      </div>
    </div>
  );
}

function useFinishedSessions(): SessionInfo[] | null {
  const [list, setList] = useState<SessionInfo[] | null>(null);
  useEffect(() => {
    api.sessions(200).then((l) => setList(l.filter((s) => !["running", "starting", "analyzing"].includes(s.status)))).catch(() => setList([]));
  }, []);
  return list;
}

function SessionSelect({ value, onChange, list, label }: { value: string; onChange: (v: string) => void; list: SessionInfo[]; label: string }) {
  return (
    <label className="block min-w-0 flex-1">
      <span className="eyebrow mb-1 block">{label}</span>
      <select className="input" value={value} onChange={(e) => onChange(e.target.value)}>
        <option value="">Select a finished session…</option>
        {list.map((s) => (
          <option key={s.id} value={s.id}>
            {s.label ?? s.id} — {s.verdict?.replace(/_/g, " ")} — {timeAgo(s.created_at)} ({s.id})
          </option>
        ))}
      </select>
    </label>
  );
}

function compareModel(side: Comparison["a"]): GraphModel {
  const s = { ...side.snapshot, findings: [] } as unknown as Snapshot;
  // final state: show exited threads and idle locks so a clean run is not an empty picture
  return buildGraph(s, "resource", { showExited: true, showIdle: true });
}

function CompareTab() {
  const list = useFinishedSessions();
  const active = useStore(sessionStore, (s) => s.info);
  const [params, setParams] = useSearchParams();
  const [a, setA] = useState(params.get("a") ?? "");
  const [b, setB] = useState(params.get("b") ?? "");
  const [cmp, setCmp] = useState<Comparison | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    if (!list || a || b) return;
    // sensible default: the active session and the latest session of its paired scenario
    if (active?.scenario_id) {
      const self = list.find((s) => s.id === active.id);
      const scenarios = list.filter((s) => s.scenario_id && s.scenario_id !== active.scenario_id);
      if (self) {
        setA(self.id);
        const other = scenarios[0];
        if (other) setB(other.id);
      }
    }
  }, [list, active, a, b]);

  useEffect(() => {
    if (!a || !b) return setCmp(null);
    setError(null);
    setParams({ tab: "compare", a, b }, { replace: true });
    api.compare(a, b).then(setCmp).catch((e) => setError(e.message));
  }, [a, b, setParams]);

  if (!list) return <Skeleton className="h-64" />;
  return (
    <div className="space-y-5">
      <Card className="flex flex-wrap items-end gap-4 p-4">
        <SessionSelect label="A — original" value={a} onChange={setA} list={list} />
        <button className="btn mb-0.5" onClick={() => { setA(b); setB(a); }} aria-label="Swap A and B">⇄</button>
        <SessionSelect label="B — corrected" value={b} onChange={setB} list={list} />
      </Card>
      {error && <div className="text-sm text-crit">{error}</div>}
      {!cmp && !error && (
        <Card>
          <EmptyState icon={<GitCompare size={22} />} title="Pick two finished sessions" body="Tip: in the Test Lab, “Run original + fix” launches a scenario and its corrected twin and opens this comparison automatically." />
        </Card>
      )}
      {cmp && (
        <>
          <div className="grid gap-5 lg:grid-cols-2">
            {(["a", "b"] as const).map((k, i) => {
              const side = cmp[k];
              return (
                <motion.div key={k + side.id} initial={{ opacity: 0, x: i ? 16 : -16 }} animate={{ opacity: 1, x: 0 }} transition={{ type: "spring", stiffness: 260, damping: 30 }}>
                  <Card className="overflow-hidden" glow={side.confirmed_deadlocks ? "crit" : undefined}>
                    <CardHeader eyebrow={k === "a" ? "A · original" : "B · corrected"} title={side.label ?? side.id} actions={<VerdictPill verdict={side.verdict} />} />
                    <Graph2D model={compareModel(side)} mode="resource" height={260} compact />
                    <div className="flex flex-wrap gap-2 border-t hairline px-4 py-2.5 text-xs text-fg-2">
                      <span className="chip">{side.completed ? "completed" : side.status}</span>
                      <span className="chip">{side.events.toLocaleString()} events</span>
                      {side.findings_list.slice(0, 3).map((f) => (
                        <span key={f.id} className="chip" style={{ color: f.severity === "critical" ? "var(--crit)" : "var(--warn)" }}>{f.title.slice(0, 48)}</span>
                      ))}
                    </div>
                  </Card>
                </motion.div>
              );
            })}
          </div>
          <Card>
            <CardHeader eyebrow="Measured differences" title="Comparison" />
            <div className="overflow-x-auto">
              <table className="w-full text-sm">
                <thead>
                  <tr className="eyebrow border-b hairline text-left">
                    <th className="px-4 py-2 font-normal">Metric</th>
                    <th className="px-4 py-2 font-normal">A</th>
                    <th className="px-4 py-2 font-normal">B</th>
                    <th className="px-4 py-2 font-normal">Change</th>
                  </tr>
                </thead>
                <tbody>
                  {cmp.rows.map((r, i) => (
                    <motion.tr key={r.key} initial={{ opacity: 0 }} animate={{ opacity: 1 }} transition={{ delay: i * 0.025 }} className="border-b hairline last:border-0">
                      <td className="px-4 py-2 text-fg-2">{r.label}</td>
                      <td className="mono px-4 py-2">{fmtCell(r.a)}</td>
                      <td className="mono px-4 py-2">{fmtCell(r.b)}</td>
                      <td className="px-4 py-2">
                        {r.change === "improved" && <span className="inline-flex items-center gap-1 text-ok"><ArrowDownRight size={14} /> improved{r.delta !== null && ` (${r.delta > 0 ? "+" : ""}${r.delta})`}</span>}
                        {r.change === "regressed" && <span className="inline-flex items-center gap-1 text-crit"><ArrowUpRight size={14} /> worse{r.delta !== null && ` (${r.delta > 0 ? "+" : ""}${r.delta})`}</span>}
                        {!r.change && (
                          <span className="text-fg-3">
                            {r.note ? r.note : r.delta !== null && r.delta !== 0 ? `${r.delta > 0 ? "+" : ""}${r.delta}` : "—"}
                          </span>
                        )}
                      </td>
                    </motion.tr>
                  ))}
                </tbody>
              </table>
            </div>
            <div className="space-y-2 border-t hairline p-4">
              {cmp.observations.map((o, i) => (
                <div key={i} className="flex gap-2 text-sm"><span className="mt-1.5 h-1.5 w-1.5 rounded-full bg-cyan" />{o}</div>
              ))}
              <p className="pt-1 text-xs text-fg-3">{cmp.disclaimer}</p>
            </div>
          </Card>
        </>
      )}
    </div>
  );
}

function fmtCell(v: unknown): string {
  if (v === null || v === undefined) return "—";
  if (typeof v === "boolean") return v ? "yes" : "no";
  if (typeof v === "number") return Number.isInteger(v) ? v.toLocaleString() : v.toFixed(3);
  return String(v);
}

function CrossRunTab() {
  const list = useFinishedSessions();
  const [picked, setPicked] = useState<Set<string>>(new Set());
  const [merged, setMerged] = useState<MergedLockOrder | null>(null);
  const [error, setError] = useState<string | null>(null);
  useEffect(() => {
    if (picked.size === 0) return setMerged(null);
    setError(null);
    api.lockOrder([...picked]).then(setMerged).catch((e) => setError(e.message));
  }, [picked]);
  const model: GraphModel = useMemo(() => {
    if (!merged) return { nodes: [], edges: [], cycleCount: 0 };
    const inCycle = new Set(merged.cycles.flatMap((c) => c.nodes));
    const cycleEdges = new Set(merged.cycles.flatMap((c) => c.nodes.map((n, i) => `${n}>${c.nodes[(i + 1) % c.nodes.length]}`)));
    return {
      nodes: merged.nodes.map((n, i) => ({ id: `l:${i}:${n.key}`, kind: "lock" as const, ref: i, label: n.label ?? n.key, sub: n.key, classes: ["lock", ...(inCycle.has(n.key) ? ["potential"] : [])] })),
      edges: merged.edges.map((e) => {
        const si = merged.nodes.findIndex((n) => n.key === e.from);
        const ti = merged.nodes.findIndex((n) => n.key === e.to);
        return {
          id: `m:${e.from}>${e.to}`,
          source: `l:${si}:${e.from}`,
          target: `l:${ti}:${e.to}`,
          kind: "order" as const,
          label: `${e.sessions.length} run${e.sessions.length > 1 ? "s" : ""}`,
          classes: ["order", ...(cycleEdges.has(`${e.from}>${e.to}`) ? ["potential"] : [])],
        };
      }),
      cycleCount: merged.cycles.length,
    };
  }, [merged]);
  if (!list) return <Skeleton className="h-64" />;
  return (
    <div className="grid gap-5 xl:grid-cols-[380px_1fr]">
      <Card>
        <CardHeader eyebrow="Select runs" title={`${picked.size} selected`} />
        <div className="max-h-[560px] divide-y divide-[var(--line)] overflow-y-auto">
          {list.map((s) => (
            <label key={s.id} className="flex cursor-pointer items-center gap-3 px-4 py-2.5 hover:bg-surface-2">
              <input
                type="checkbox"
                checked={picked.has(s.id)}
                onChange={() =>
                  setPicked((p) => {
                    const n = new Set(p);
                    if (n.has(s.id)) n.delete(s.id);
                    else n.add(s.id);
                    return n;
                  })
                }
                className="accent-[var(--cyan)]"
              />
              <span className="min-w-0 flex-1">
                <span className="block truncate text-sm">{s.label ?? s.id}</span>
                <span className="mono block text-[11px] text-fg-3">{s.id}</span>
              </span>
              <VerdictPill verdict={s.verdict} />
            </label>
          ))}
        </div>
        <div className="border-t hairline p-3 text-xs text-fg-3">Try “Lock order A→B” and “Lock order B→A” from the Test Lab: each run is clean on its own.</div>
      </Card>
      <div className="space-y-4">
        {error && <div className="text-sm text-crit">{error}</div>}
        <Card className="overflow-hidden">
          <div className="border-b hairline px-4 py-2.5">
            <GraphLegend mode="lockorder" />
          </div>
          <Graph2D model={model} mode="lockorder" height={420} />
        </Card>
        {merged?.cycles.map((c, i) => (
          <Card key={i} className="p-4">
            <div className="flex flex-wrap items-center gap-2">
              <VerdictPill verdict="potential_deadlock" />
              {c.cross_session && <span className="chip text-warn">only visible by merging runs</span>}
            </div>
            <div className="mono mt-2 text-sm text-warn">{[...c.nodes, c.nodes[0]].join("  →  ")}</div>
            <p className="mt-2 text-sm text-fg-2">{c.explanation}</p>
            <div className="mono mt-2 text-[11px] text-fg-3">sessions: {c.sessions.join(", ")}</div>
          </Card>
        ))}
        {merged && merged.cycles.length === 0 && (
          <Card className="p-6 text-center text-sm text-fg-2">No lock-order cycle across the selected runs.</Card>
        )}
      </div>
    </div>
  );
}
