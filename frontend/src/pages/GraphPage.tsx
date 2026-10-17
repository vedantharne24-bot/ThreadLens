import { motion } from "motion/react";
import { ArrowRight, Box, Info, Square } from "lucide-react";
import { type ReactNode, Suspense, lazy, useEffect, useMemo, useState } from "react";
import { useNavigate, useSearchParams } from "react-router-dom";

import { SessionGate } from "@/components/SessionGate";
import { Card, CardHeader, KV, LockChip, PageHeader, Segmented, SeverityBadge, Skeleton, StateBadge, ThreadChip, Toggle } from "@/components/ui";
import { hasWebGL, setPrefs, usePrefs } from "@/hooks/usePrefs";
import { api } from "@/services/api";
import { sessionStore } from "@/services/session";
import { useStore } from "@/services/store";
import type { EventView, Snapshot } from "@/types/api";
import { TYPE_GROUPS, fmtDuration, lockMap, modeWord, threadMap, typeColor } from "@/utils/format";
import { type GraphMode, MODE_INFO, buildGraph } from "@/utils/graph";
import { Graph2D, GraphLegend } from "@/visualization/Graph2D";

const Graph3D = lazy(() => import("@/visualization/Graph3D"));
const SEM_TYPES = TYPE_GROUPS.Semaphores.join(",");

export default function GraphPage() {
  return (
    <SessionGate what="the lock graph">
      <GraphBody />
    </SessionGate>
  );
}

function GraphBody() {
  const snap = useStore(sessionStore, (s) => s.snapshot)!;
  const sid = useStore(sessionStore, (s) => s.activeId)!;
  const [params, setParams] = useSearchParams();
  const mode = (params.get("mode") as GraphMode) || "resource";
  const pref3d = usePrefs((p) => p.graphMode);
  const webgl = useMemo(() => hasWebGL(), []);
  const dim = webgl ? pref3d : "2d";
  const [selected, setSelected] = useState<string | null>(null);
  const [showIdle, setShowIdle] = useState(false);
  const [showExited, setShowExited] = useState(false);
  const model = useMemo(() => buildGraph(snap, mode, { showIdle, showExited }), [snap, mode, showIdle, showExited]);
  const info = MODE_INFO[mode];

  return (
    <div className="space-y-5">
      <PageHeader
        eyebrow="Lock graph"
        title={info.title}
        description={info.semantics}
        actions={
          <>
            <Segmented
              value={mode}
              onChange={(m) => {
                setSelected(null);
                setParams({ mode: m });
              }}
              options={[
                { value: "resource", label: "Resources", title: "threads + locks, current" },
                { value: "waitfor", label: "Wait-for", title: "threads only, current" },
                { value: "lockorder", label: "Lock order", title: "locks only, historical" },
              ]}
            />
            <Segmented
              value={dim}
              onChange={(v) => setPrefs({ graphMode: v })}
              options={[
                { value: "2d", label: <><Square size={13} /> 2D</> },
                { value: "3d", label: <><Box size={13} /> 3D</>, title: webgl ? "interactive 3D view" : "WebGL is not available" },
              ]}
            />
          </>
        }
      />
      {!webgl && pref3d === "3d" && (
        <div className="card flex items-center gap-2 px-4 py-2.5 text-sm text-fg-2">
          <Info size={15} className="text-info" /> WebGL is not available in this browser, so the equivalent 2D view is shown.
        </div>
      )}
      <div className="grid gap-5 xl:grid-cols-[1fr_380px]">
        <Card className="overflow-hidden">
          <div className="flex flex-wrap items-center justify-between gap-3 border-b hairline px-4 py-2.5">
            <GraphLegend mode={mode} />
            <div className="flex items-center gap-4 text-xs text-fg-2">
              <label className="flex items-center gap-2">
                <Toggle checked={showIdle} onChange={setShowIdle} label="Show idle locks" /> idle locks
              </label>
              {mode !== "lockorder" && (
                <label className="flex items-center gap-2">
                  <Toggle checked={showExited} onChange={setShowExited} label="Show exited threads" /> exited threads
                </label>
              )}
            </div>
          </div>
          <CycleBanner snap={snap} mode={mode} />
          {dim === "3d" ? (
            <Suspense fallback={<Skeleton className="h-[560px] rounded-none" />}>
              <Graph3D model={model} selected={selected} onSelect={setSelected} height={560} />
            </Suspense>
          ) : (
            <Graph2D model={model} mode={mode} selected={selected} onSelect={setSelected} height={560} />
          )}
        </Card>
        <NodePanel sid={sid} snap={snap} selected={selected} />
      </div>
    </div>
  );
}

function CycleBanner({ snap, mode }: { snap: Snapshot; mode: GraphMode }) {
  const threads = threadMap(snap);
  const locks = lockMap(snap);
  const cycles = mode === "lockorder" ? snap.lock_order.cycles : snap.wait_for.cycles;
  if (!cycles.length) return null;
  const crit = mode !== "lockorder";
  return (
    <div className="border-b hairline px-4 py-2 text-sm" style={{ background: `color-mix(in oklab, ${crit ? "var(--crit)" : "var(--warn)"} 8%, transparent)` }}>
      {cycles.map((c, i) => (
        <div key={i} className="mono text-xs" style={{ color: crit ? "var(--crit)" : "var(--warn)" }}>
          {crit ? "wait-for cycle: " : "lock-order cycle (potential): "}
          {[...c, c[0]].map((n) => (mode === "lockorder" ? locks.get(n)?.label ?? `L${n}` : threads.get(n)?.label ?? `T${n}`)).join("  →  ")}
        </div>
      ))}
    </div>
  );
}

function NodePanel({ sid, snap, selected }: { sid: string; snap: Snapshot; selected: string | null }) {
  const navigate = useNavigate();
  const [events, setEvents] = useState<EventView[] | null>(null);
  const threads = threadMap(snap);
  const locks = lockMap(snap);
  const kind = selected?.startsWith("t:") ? "thread" : selected?.startsWith("l:") ? "lock" : selected?.startsWith("s:") ? "sem" : null;
  const ref = selected ? Number(selected.slice(2)) : null;
  const sem = kind === "sem" && ref !== null ? (snap.semaphores ?? [])[ref] : undefined;
  const eventQuery: Record<string, string> =
    kind === "thread" ? { threads: String(ref) } : kind === "sem" ? { types: SEM_TYPES, q: sem?.label ?? "" } : { locks: String(ref) };

  useEffect(() => {
    if (!selected || ref === null) return setEvents(null);
    setEvents(null);
    const q = eventQuery;
    api
      .events(sid, { ...q, order: "desc", limit: 25 })
      .then((p) => setEvents(p.events))
      .catch(() => setEvents([]));
    // re-fetch when the snapshot advances so live evidence stays current
  }, [sid, selected, kind, ref, Math.floor(snap.seq / 25)]);

  const related = snap.findings.filter((f) =>
    kind === "thread"
      ? f.threads.includes(ref!)
      : kind === "lock"
        ? f.locks.includes(ref!)
        : kind === "sem" && sem
          ? ((f.metrics.semaphores as string[] | undefined) ?? []).includes(sem.label)
          : false,
  );

  return (
    <Card className="flex min-h-[300px] flex-col">
      <CardHeader eyebrow="Evidence" title={selected ? "Selected node" : "Select a node"} />
      {/* enter-only transitions: content never waits for an exit animation (background tabs throttle rAF) */}
      <>
        {!selected ? (
          <motion.div key="empty" initial={{ opacity: 0 }} animate={{ opacity: 1 }} className="p-5 text-sm text-fg-2">
            Click a thread or lock in the graph to inspect its state and the recorded events behind it. Graph state is computed by the backend analysis engine; the visualization only renders it.
          </motion.div>
        ) : (
          <motion.div key={selected} initial={{ opacity: 0, x: 8 }} animate={{ opacity: 1, x: 0 }} className="min-h-0 flex-1 overflow-y-auto p-4">
            {kind === "thread" && threads.get(ref!) && (() => {
              const t = threads.get(ref!)!;
              return (
                <div className="space-y-3">
                  <div className="flex items-center justify-between">
                    <ThreadChip index={t.index} label={t.label} />
                    <StateBadge state={t.state} inCycle={t.in_cycle} />
                  </div>
                  <KV
                    items={[
                      ["TID", <span className="mono">{t.tid}</span>],
                      [
                        "Holds",
                        t.held.length ? (
                          <span className="flex flex-wrap gap-1">
                            {t.held.map((h) => (
                              <LockChip key={h.lock} id={h.lock} label={`${locks.get(h.lock)?.label ?? `L${h.lock}`}${h.mode !== "x" ? ` (${modeWord(h.mode)})` : ""}`} />
                            ))}
                          </span>
                        ) : (
                          "nothing"
                        ),
                      ],
                      [
                        "Waiting",
                        t.wait
                          ? `${t.wait.kind === "sem" ? `semaphore ${t.wait.sem_label ?? t.wait.sem}` : t.wait.kind}${
                              t.wait.lock !== null ? ` on ${locks.get(t.wait.lock)?.label}` : ""
                            }${t.wait.mode ? ` to ${modeWord(t.wait.mode)}` : ""} since #${t.wait.since_seq}`
                          : "no",
                      ],
                      ["Total wait", fmtDuration(t.wait_ns_total)],
                    ]}
                  />
                </div>
              );
            })()}
            {kind === "lock" && locks.get(ref!) && (() => {
              const l = locks.get(ref!)!;
              const owner = l.owner !== null ? threads.get(l.owner) : null;
              const readers = l.readers ?? [];
              return (
                <div className="space-y-3">
                  <LockChip id={l.id} label={l.label} />
                  <KV
                    items={[
                      ["Type", l.kind],
                      ["Address", <span className="mono text-xs">{l.addr}</span>],
                      ["Symbol", l.symbol ?? "—"],
                      [l.primitive === "rwlock" ? "Writer" : "Owner", owner ? <ThreadChip index={owner.index} label={owner.label} /> : l.primitive === "rwlock" ? "none" : "free"],
                      ...(l.primitive === "rwlock"
                        ? ([
                            [
                              "Readers",
                              readers.length ? (
                                <span className="flex flex-wrap gap-1">
                                  {readers.map((r) => (
                                    <ThreadChip key={r.thread} index={r.thread} label={`${threads.get(r.thread)?.label ?? `T${r.thread}`}${r.count > 1 ? ` ×${r.count}` : ""}`} />
                                  ))}
                                </span>
                              ) : (
                                "none"
                              ),
                            ],
                          ] as [string, ReactNode][])
                        : []),
                      ["Waiters", l.waiters.length ? <span className="flex flex-wrap gap-1">{l.waiters.map((w) => <ThreadChip key={w} index={w} label={threads.get(w)?.label} />)}</span> : "none"],
                      ["Acquisitions", `${l.acquisitions} (${l.contended} contended)`],
                      ["Wait time", `${fmtDuration(l.wait_ns_total)} total · max ${fmtDuration(l.wait_ns_max)}`],
                      ["Hold time", `max ${fmtDuration(l.hold_ns_max)}`],
                    ]}
                  />
                </div>
              );
            })()}
            {kind === "sem" && sem && (
              <div className="space-y-3">
                <span className="mono text-sm text-violet">{sem.label}</span>
                <KV
                  items={[
                    ["Type", sem.pshared ? "POSIX semaphore (process-shared)" : "POSIX semaphore"],
                    ["Address", <span className="mono text-xs">{sem.addr}</span>],
                    ["Count", sem.value === null ? "unknown (initial value not observed)" : `${sem.value} = ${sem.init_value} initial + ${sem.posts} posts − ${sem.waits} waits`],
                    ["Waiters", sem.waiters.length ? <span className="flex flex-wrap gap-1">{sem.waiters.map((w) => <ThreadChip key={w} index={w} label={threads.get(w)?.label} />)}</span> : "none"],
                    ["Posts with no waiter", String(sem.posts_without_waiters)],
                    ["Owner", "none — any thread may post, so a semaphore wait adds no wait-for edge"],
                  ]}
                />
              </div>
            )}
            {related.length > 0 && (
              <div className="mt-4 space-y-2">
                <div className="eyebrow">Findings</div>
                {related.map((f) => (
                  <button key={f.id} className="card-raised flex w-full items-start gap-2 p-2.5 text-left hover:border-line-strong" onClick={() => navigate(`/app/diagnostics?f=${f.id}`)}>
                    <SeverityBadge severity={f.severity} />
                    <span className="min-w-0 flex-1 text-sm">{f.title}</span>
                  </button>
                ))}
              </div>
            )}
            <div className="mt-4">
              <div className="eyebrow mb-2">Recent recorded events</div>
              {events === null ? (
                <Skeleton className="h-32" />
              ) : events.length === 0 ? (
                <div className="text-sm text-fg-3">No events.</div>
              ) : (
                <ol className="space-y-1">
                  {events.map((e) => (
                    <li key={e.seq} className="flex gap-2 text-xs">
                      <span className="mono w-12 shrink-0 text-fg-3">#{e.seq}</span>
                      <span className="mono w-1.5 shrink-0 self-stretch rounded-full" style={{ background: typeColor(e.type) }} />
                      <span className="text-fg-2">{e.description}</span>
                    </li>
                  ))}
                </ol>
              )}
              <button className="mt-3 inline-flex items-center gap-1 text-xs text-cyan hover:underline" onClick={() => navigate(`/app/events?${new URLSearchParams(eventQuery).toString()}`)}>
                Open in Event Explorer <ArrowRight size={12} />
              </button>
            </div>
          </motion.div>
        )}
      </>
    </Card>
  );
}
