import { AnimatePresence, motion } from "motion/react";
import { CheckCircle2, ChevronDown, History, Lightbulb, ShieldAlert, ShieldCheck, TriangleAlert } from "lucide-react";
import { useEffect, useMemo, useState } from "react";
import { Link, useSearchParams } from "react-router-dom";

import { SessionGate } from "@/components/SessionGate";
import { Card, CardHeader, KV, LockChip, PageHeader, Segmented, SeverityBadge, ThreadChip, Toggle, VerdictPill, cx } from "@/components/ui";
import { setPrefs, usePrefs } from "@/hooks/usePrefs";
import { sessionStore } from "@/services/session";
import { useStore } from "@/services/store";
import type { Finding, Snapshot } from "@/types/api";
import { SEVERITY_META, fmtDuration, lockMap, threadMap } from "@/utils/format";
import { CycleDiagram } from "@/visualization/CycleDiagram";

export default function Diagnostics() {
  return (
    <SessionGate what="diagnostics">
      <DiagnosticsBody />
    </SessionGate>
  );
}

function DiagnosticsBody() {
  const snap = useStore(sessionStore, (s) => s.snapshot)!;
  const [params] = useSearchParams();
  const focus = params.get("f");
  const showFiltered = usePrefs((p) => p.showFilteredFindings);
  const [sev, setSev] = useState<"all" | "critical" | "warning">("all");
  const [open, setOpen] = useState<Set<string>>(() => new Set(focus ? [focus] : []));
  useEffect(() => {
    if (focus) {
      setOpen((o) => new Set([...o, focus]));
      window.setTimeout(() => document.getElementById(`finding-${focus}`)?.scrollIntoView({ behavior: "smooth", block: "start" }), 250);
    }
  }, [focus]);
  useEffect(() => {
    // expand the most serious finding by default
    if (!focus && open.size === 0 && snap.findings.length) {
      const top = [...snap.findings].sort((a, b) => rank(b) - rank(a))[0];
      setOpen(new Set([top.id]));
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [snap.findings.length]);

  const list = useMemo(
    () =>
      snap.findings
        .filter((f) => showFiltered || (f.status !== "filtered" && f.severity !== "info"))
        .filter((f) => sev === "all" || f.severity === sev)
        .sort((a, b) => rank(b) - rank(a) || a.detected_seq - b.detected_seq),
    [snap.findings, showFiltered, sev],
  );
  const hidden = snap.findings.length - snap.findings.filter((f) => f.status !== "filtered" && f.severity !== "info").length;

  return (
    <div className="space-y-6">
      <PageHeader
        eyebrow="Diagnostics"
        title="Findings and their evidence"
        description="Every finding keeps the recorded events that support it. Classifications are conservative: a deadlock is only “confirmed” when every wait was observed, ownership is known, no events were lost and the cycle outlived a collector heartbeat."
        actions={
          <>
            <Segmented
              value={sev}
              onChange={setSev}
              options={[
                { value: "all", label: "All" },
                { value: "critical", label: "Critical" },
                { value: "warning", label: "Warnings" },
              ]}
            />
            <label className="flex items-center gap-2 text-xs text-fg-2">
              <Toggle checked={showFiltered} onChange={(v) => setPrefs({ showFilteredFindings: v })} label="Show filtered and informational findings" />
              filtered / info {hidden > 0 && `(${hidden})`}
            </label>
          </>
        }
      />
      <div className="grid gap-6 xl:grid-cols-[1fr_340px]">
        <div className="space-y-4">
          {list.length === 0 && (
            <Card className="p-10 text-center">
              <CheckCircle2 size={28} className="mx-auto mb-3 text-ok" />
              <div className="font-semibold">{snap.verdict.label}</div>
              <div className="mx-auto mt-1 max-w-lg text-sm text-fg-2">{snap.verdict.notes[0] ?? "Nothing to report for the selected filters."}</div>
            </Card>
          )}
          {list.map((f) => (
            <FindingCard
              key={f.id}
              f={f}
              snap={snap}
              open={open.has(f.id)}
              toggle={() =>
                setOpen((o) => {
                  const n = new Set(o);
                  if (n.has(f.id)) n.delete(f.id);
                  else n.add(f.id);
                  return n;
                })
              }
            />
          ))}
        </div>
        <CoveragePanel snap={snap} />
      </div>
    </div>
  );
}

function rank(f: Finding): number {
  const sev = { critical: 30, warning: 20, info: 10 }[f.severity];
  const st = f.status === "confirmed" ? 5 : f.status === "suspected" ? 4 : f.status === "active" ? 3 : 0;
  return sev + st;
}

const STATUS_TEXT: Record<Finding["status"], string> = {
  suspected: "cycle detected — awaiting confirmation",
  confirmed: "confirmed",
  active: "active",
  resolved: "resolved",
  filtered: "filtered (likely false positive)",
};

function FindingCard({ f, snap, open, toggle }: { f: Finding; snap: Snapshot; open: boolean; toggle: () => void }) {
  const threads = threadMap(snap);
  const locks = lockMap(snap);
  const m = SEVERITY_META[f.severity];
  return (
    <motion.div layout id={`finding-${f.id}`} className="card overflow-hidden" style={{ boxShadow: f.severity === "critical" && f.status !== "resolved" ? "var(--glow-crit)" : undefined }}>
      <button className="flex w-full items-start gap-3 p-4 text-left" onClick={toggle} aria-expanded={open}>
        <span className="mt-0.5 h-9 w-1 shrink-0 rounded-full" style={{ background: m.color }} />
        <div className="min-w-0 flex-1">
          <div className="flex flex-wrap items-center gap-2">
            <SeverityBadge severity={f.severity} />
            <VerdictPill verdict={f.classification} />
            <span className="chip">{STATUS_TEXT[f.status]}</span>
            <span className="mono text-[11px] text-fg-3">{f.id} · detected at #{f.detected_seq}</span>
          </div>
          <h3 className="mt-2 text-[15px] font-semibold leading-snug">{f.title}</h3>
          {!open && <p className="mt-1 line-clamp-2 text-sm text-fg-2">{f.summary}</p>}
        </div>
        <ChevronDown size={18} className={cx("mt-1 shrink-0 text-fg-3 transition-transform", open && "rotate-180")} />
      </button>
      <AnimatePresence initial={false}>
        {open && (
          <motion.div initial={{ height: 0, opacity: 0 }} animate={{ height: "auto", opacity: 1 }} exit={{ height: 0, opacity: 0 }} transition={{ duration: 0.28, ease: [0.16, 1, 0.3, 1] }}>
            <div className="space-y-6 border-t hairline p-5">
              <div className="grid gap-6 lg:grid-cols-[auto_1fr]">
                {f.cycle && (
                  <div className="card-raised grid place-items-center p-2">
                    <CycleDiagram finding={f} threads={threads} locks={locks} />
                    <div className="mono px-2 pb-2 text-center text-[11px] text-fg-3">{f.cycle.type === "wait_for" ? "wait-for cycle" : "lock-order cycle"}</div>
                  </div>
                )}
                <div className="min-w-0 space-y-4">
                  <div>
                    <div className="eyebrow mb-2">Explanation</div>
                    <div className="space-y-2 text-sm leading-relaxed text-fg-2">
                      {f.explanation.map((p, i) => (
                        <p key={i}>{p}</p>
                      ))}
                    </div>
                  </div>
                  <div className="flex flex-wrap gap-4">
                    {f.threads.length > 0 && (
                      <div>
                        <div className="eyebrow mb-1.5">Threads</div>
                        <div className="flex flex-wrap gap-1.5">
                          {f.threads.map((t) => (
                            <ThreadChip key={t} index={t} label={threads.get(t)?.label} />
                          ))}
                        </div>
                      </div>
                    )}
                    {f.locks.length > 0 && (
                      <div>
                        <div className="eyebrow mb-1.5">Locks</div>
                        <div className="flex flex-wrap gap-1.5">
                          {f.locks.map((l) => (
                            <LockChip key={l} id={l} label={locks.get(l)?.label} />
                          ))}
                        </div>
                      </div>
                    )}
                  </div>
                  {f.filter_reason && (
                    <div className="card-raised p-3 text-sm text-fg-2">
                      <span className="font-medium text-fg">Filtered: </span>
                      {f.filter_reason}
                    </div>
                  )}
                </div>
              </div>

              {f.evidence.length > 0 && (
                <div>
                  <div className="eyebrow mb-3">Evidence timeline</div>
                  <ol className="relative ml-2 border-l hairline">
                    {f.evidence.map((e, i) => {
                      const color = e.role === "trigger" ? "var(--crit)" : e.role === "cause" ? "var(--warn)" : "var(--fg-3)";
                      return (
                        <motion.li key={`${e.seq}-${i}`} initial={{ opacity: 0, x: -6 }} animate={{ opacity: 1, x: 0 }} transition={{ delay: i * 0.05 }} className="relative mb-3 pl-5 last:mb-0">
                          <span className="absolute -left-[5px] top-1.5 h-2.5 w-2.5 rounded-full border-2" style={{ borderColor: color, background: "var(--surface)" }} />
                          <div className="flex flex-wrap items-baseline gap-x-2">
                            <span className="mono text-xs text-fg-3">#{e.seq}</span>
                            <span className="mono text-[10px] uppercase" style={{ color }}>
                              {e.role}
                            </span>
                            <span className="text-sm">{e.description}</span>
                          </div>
                          <div className="mt-0.5 flex flex-wrap items-center gap-x-3 text-xs text-fg-3">
                            {e.site && <span className="mono text-cyan">{e.site}</span>}
                            {!e.site && e.caller && <span className="mono">{e.caller}</span>}
                            {e.seq >= 0 && (
                              <Link to={`/app/replay?seq=${e.seq}`} className="inline-flex items-center gap-1 hover:text-fg">
                                <History size={11} /> replay to here
                              </Link>
                            )}
                            <Link to={`/app/events?seq=${e.seq}`} className="hover:text-fg">
                              event details
                            </Link>
                          </div>
                        </motion.li>
                      );
                    })}
                  </ol>
                </div>
              )}

              <div className="grid gap-5 lg:grid-cols-2">
                <div>
                  <div className="eyebrow mb-2 flex items-center gap-1.5">
                    <Lightbulb size={12} /> Suggested investigation / repair
                  </div>
                  <ul className="space-y-2 text-sm text-fg-2">
                    {f.remedies.map((r, i) => (
                      <li key={i} className="flex gap-2">
                        <span className="mt-1.5 h-1.5 w-1.5 shrink-0 rounded-full bg-cyan" />
                        {r}
                      </li>
                    ))}
                  </ul>
                  <p className="mt-2 text-xs text-fg-3">Suggestions must be verified by re-running and comparing; they are not guaranteed fixes.</p>
                </div>
                <div>
                  <div className="eyebrow mb-2 flex items-center gap-1.5">
                    <TriangleAlert size={12} /> Limitations and uncertainty
                  </div>
                  <ul className="space-y-2 text-xs leading-relaxed text-fg-3">
                    {f.limitations.map((l, i) => (
                      <li key={i}>{l}</li>
                    ))}
                  </ul>
                </div>
              </div>

              {Object.keys(f.metrics).length > 0 && (
                <div>
                  <div className="eyebrow mb-2">Metrics</div>
                  <div className="flex flex-wrap gap-2">
                    {Object.entries(f.metrics).map(([k, v]) => (
                      <span key={k} className="chip mono">
                        <span className="text-fg-3">{k}</span> {formatMetric(k, v)}
                      </span>
                    ))}
                  </div>
                </div>
              )}
            </div>
          </motion.div>
        )}
      </AnimatePresence>
    </motion.div>
  );
}

function formatMetric(k: string, v: unknown): string {
  if (typeof v === "number" && (k.endsWith("_ns") || k === "formed_ts")) return k === "formed_ts" ? String(v) : fmtDuration(v);
  if (typeof v === "number" && k.includes("ratio")) return `${(v * 100).toFixed(1)}%`;
  if (typeof v === "boolean") return v ? "yes" : "no";
  return String(v);
}

function CoveragePanel({ snap }: { snap: Snapshot }) {
  const c = snap.coverage;
  return (
    <div className="space-y-4">
      <Card>
        <CardHeader icon={c.complete ? <ShieldCheck size={15} className="text-ok" /> : <ShieldAlert size={15} className="text-warn" />} eyebrow="Trust" title="Collection coverage" />
        <div className="space-y-3 p-4">
          <p className="text-sm text-fg-2">
            {c.complete
              ? "Every in-band record arrived in sequence with no reported loss. Findings can rely on complete ordering information."
              : "Some information is missing. Findings that depend on it are reported as inconclusive rather than confirmed."}
          </p>
          <KV
            items={[
              ["Lost (ring full)", c.lost_events],
              ["Sequence gaps", `${c.seq_gaps} (${c.missing_events} missing)`],
              ["Duplicates / reordered", `${c.duplicates} / ${c.reordered}`],
              ["Anomalies", c.anomalies],
              ["Heartbeats", c.heartbeats],
              ["Late-discovered threads", c.late_threads],
              ["glibc owner cross-checks", `${c.owner_checks} (${c.owner_mismatches} mismatches)`],
              ["Clean collector exit", c.clean_exit ? "yes" : "no (killed or crashed)"],
            ]}
          />
          {c.anomaly_examples.length > 0 && (
            <div className="card-raised space-y-1 p-2.5 text-xs text-fg-2">
              {c.anomaly_examples.slice(0, 5).map((a, i) => (
                <div key={i}>
                  <span className="mono text-fg-3">#{a.seq}</span> {a.detail}
                </div>
              ))}
            </div>
          )}
          <p className="text-[11px] leading-relaxed text-fg-3">
            Owner mismatches are expected occasionally: the owner TID is read from glibc's mutex without stopping the program, so it can race with an unlock by a few nanoseconds.
          </p>
        </div>
      </Card>
      <Card>
        <CardHeader eyebrow="Scope" title="What is observed" />
        <div className="space-y-2 p-4 text-xs leading-relaxed text-fg-2">
          <p>Observed: pthread mutexes (lock, trylock, timedlock, unlock, init, destroy), condition variables, thread create/join/detach/exit/cancel, barriers.</p>
          <p className="text-fg-3">Not observed: rwlocks, spinlocks, semaphores, futex/atomic-based locks, glibc-internal locks, statically linked programs and child processes.</p>
        </div>
      </Card>
    </div>
  );
}
