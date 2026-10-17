import { motion } from "motion/react";
import { ArrowRight, GitBranch } from "lucide-react";
import { type ReactNode, useEffect, useMemo, useState } from "react";
import { useNavigate } from "react-router-dom";

import { SessionGate } from "@/components/SessionGate";
import { Card, CardHeader, LockChip, PageHeader, Segmented, Skeleton, StateBadge, ThreadChip } from "@/components/ui";
import { api } from "@/services/api";
import { sessionStore } from "@/services/session";
import { useStore } from "@/services/store";
import type { ThreadView, Timeline } from "@/types/api";
import { fmtDuration, lockHolders, lockMap, modeWord, threadMap } from "@/utils/format";
import { ThreadTimeline } from "@/visualization/ThreadTimeline";

export default function Threads() {
  return (
    <SessionGate what="the thread monitor">
      <ThreadsBody />
    </SessionGate>
  );
}

function waitText(t: ThreadView, threads: Map<number, ThreadView>, locks: ReturnType<typeof lockMap>): ReactNode {
  const w = t.wait;
  if (!w) return <span className="text-fg-3">—</span>;
  if (w.kind === "lock" && w.lock !== null) {
    const l = locks.get(w.lock);
    const owner = l?.owner !== null && l?.owner !== undefined ? threads.get(l.owner) : undefined;
    return (
      <span className="inline-flex flex-wrap items-center gap-1.5">
        <LockChip id={w.lock} label={l?.label} tone="var(--warn)" />
        {owner ? (
          <>
            <span className="text-fg-3">held by</span> <ThreadChip index={owner.index} label={owner.label} />
          </>
        ) : (
          <span className="text-fg-3">(free — acquiring)</span>
        )}
        {w.timed && <span className="chip">timed</span>}
      </span>
    );
  }
  if (w.kind === "rwlock" && w.lock !== null) {
    const l = locks.get(w.lock);
    // a writer waits for every holder; a reader only for the writer
    const blockers = l ? lockHolders(l).filter((h) => h.thread !== t.index || w.mode === "w") : [];
    const relevant = w.mode === "w" ? blockers : blockers.filter((h) => h.mode !== "r");
    return (
      <span className="inline-flex flex-wrap items-center gap-1.5">
        <LockChip id={w.lock} label={`${l?.label ?? `L${w.lock}`} (${modeWord(w.mode)})`} tone="var(--warn)" />
        {relevant.length ? (
          <>
            <span className="text-fg-3">{relevant.some((h) => h.mode === "r") ? "read by" : "write-held by"}</span>
            {relevant.map((h) => (
              <ThreadChip key={h.thread} index={h.thread} label={h.thread === t.index ? `${threads.get(h.thread)?.label ?? "self"} (itself)` : threads.get(h.thread)?.label} />
            ))}
          </>
        ) : (
          <span className="text-fg-3">(compatible — acquiring)</span>
        )}
        {w.timed && <span className="chip">timed</span>}
      </span>
    );
  }
  if (w.kind === "sem") {
    return (
      <span className="inline-flex flex-wrap items-center gap-1.5">
        <span className="mono text-xs text-violet">semaphore {w.sem_label ?? w.sem}</span>
        <span className="text-fg-3">(no owner)</span>
        {w.timed && <span className="chip">timed</span>}
      </span>
    );
  }
  if (w.kind === "join") {
    const target = w.target !== null ? threads.get(w.target) : undefined;
    return (
      <span className="inline-flex items-center gap-1.5">
        <span className="text-fg-3">join</span> {target ? <ThreadChip index={target.index} label={target.label} /> : "?"}
      </span>
    );
  }
  if (w.kind === "cond") {
    return (
      <span className="inline-flex flex-wrap items-center gap-1.5">
        <span className="mono text-xs text-violet">cond {w.cond}</span>
        {w.lock !== null && <LockChip id={w.lock} label={locks.get(w.lock)?.label} />}
        {w.signaled && <span className="chip">{w.signaled}ed</span>}
        {w.timed && <span className="chip">timed</span>}
      </span>
    );
  }
  return <span className="text-info">barrier</span>;
}

function ThreadsBody() {
  const snap = useStore(sessionStore, (s) => s.snapshot)!;
  const info = useStore(sessionStore, (s) => s.info);
  const navigate = useNavigate();
  const [filter, setFilter] = useState<"all" | "alive" | "blocked">("all");
  const [timeline, setTimeline] = useState<Timeline | null>(null);
  const threads = useMemo(() => threadMap(snap), [snap]);
  const locks = useMemo(() => lockMap(snap), [snap]);
  const running = info?.status === "running" || info?.status === "starting";
  const id = info?.id;

  useEffect(() => {
    if (!id) return;
    let stop = false;
    const load = () => api.timeline(id, 800).then((t) => !stop && setTimeline(t)).catch(() => undefined);
    void load();
    if (!running) return () => void (stop = true);
    const timer = window.setInterval(load, 1500);
    return () => {
      stop = true;
      window.clearInterval(timer);
    };
  }, [id, running]);

  const rows = snap.threads.filter((t) =>
    filter === "all" ? true : filter === "alive" ? t.state !== "exited" : t.state === "blocked_lock" || t.state === "blocked_join" || t.in_cycle,
  );
  const cycle = new Set(snap.threads.filter((t) => t.in_cycle).map((t) => t.index));

  return (
    <div className="space-y-6">
      <PageHeader
        eyebrow="Live threads"
        title="Threads and their synchronization state"
        description="Observed states come from intercepted pthread calls: a thread is “blocked on lock” between its LOCK_REQUEST / RW_REQUEST and the matching acquisition, “waiting (condvar)” inside pthread_cond_wait, “waiting (semaphore)” inside sem_wait, and so on."
        actions={
          <Segmented
            value={filter}
            onChange={setFilter}
            options={[
              { value: "all", label: `All ${snap.threads.length}` },
              { value: "alive", label: "Alive" },
              { value: "blocked", label: "Blocked" },
            ]}
          />
        }
      />
      <div className="grid gap-3 md:grid-cols-2 2xl:grid-cols-3">
        {rows.map((t, i) => (
          <motion.div
            key={t.index}
            layout
            initial={{ opacity: 0, y: 8 }}
            animate={{ opacity: 1, y: 0 }}
            transition={{ delay: Math.min(i, 12) * 0.03 }}
            className="card p-4"
            style={{ boxShadow: t.in_cycle ? "var(--glow-crit)" : undefined }}
          >
            <div className="flex items-start justify-between gap-3">
              <div className="min-w-0">
                <div className="flex items-center gap-2">
                  <span className="mono text-xs text-fg-3">T{t.index}</span>
                  <span className="truncate font-semibold">{t.label}</span>
                </div>
                <div className="mono mt-0.5 text-[11px] text-fg-3">
                  tid {t.tid}
                  {t.parent !== null && ` · parent T${t.parent}`}
                  {t.detached && " · detached"}
                  {t.late && " · discovered late"}
                </div>
              </div>
              <StateBadge state={t.state} inCycle={t.in_cycle} />
            </div>
            <dl className="mt-3 grid grid-cols-[88px_1fr] gap-x-3 gap-y-2 text-sm">
              <dt className="text-fg-3">Holds</dt>
              <dd className="flex flex-wrap gap-1.5">
                {t.held.length ? (
                  t.held.map((h) => (
                    <LockChip key={h.lock} id={h.lock} label={`${locks.get(h.lock)?.label ?? `L${h.lock}`}${h.mode !== "x" ? ` (${modeWord(h.mode)})` : ""}`} />
                  ))
                ) : (
                  <span className="text-fg-3">nothing</span>
                )}
              </dd>
              <dt className="text-fg-3">Waiting for</dt>
              <dd>{waitText(t, threads, locks)}</dd>
              <dt className="text-fg-3">Last event</dt>
              <dd className="mono truncate text-xs text-fg-2">
                {t.last_type ?? "—"} <span className="text-fg-3">#{t.last_seq}</span>
              </dd>
              <dt className="text-fg-3">Activity</dt>
              <dd className="text-xs text-fg-2">
                {t.events.toLocaleString()} events · {t.acquisitions.toLocaleString()} acquisitions · waited {fmtDuration(t.wait_ns_total)}
              </dd>
              {t.state === "exited" && (
                <>
                  <dt className="text-fg-3">Exit</dt>
                  <dd className="text-xs text-fg-2">
                    {t.terminated_by_process_exit ? "ended with the process" : ["returned", "pthread_exit()", "cancelled / unknown path", "process exit"][t.exit_kind ?? 2]}
                    {t.ended_ts && ` after ${fmtDuration(t.ended_ts - t.started_ts)}`}
                  </dd>
                </>
              )}
            </dl>
            <button className="mt-3 inline-flex items-center gap-1 text-xs text-cyan hover:underline" onClick={() => navigate(`/app/events?threads=${t.index}`)}>
              Events of this thread <ArrowRight size={12} />
            </button>
          </motion.div>
        ))}
      </div>
      <Card>
        <CardHeader icon={<GitBranch size={15} />} eyebrow="Timeline" title="Thread activity lanes" />
        <div className="p-4">
          {timeline ? (
            timeline.lanes.length ? (
              <ThreadTimeline timeline={timeline} locks={locks} highlight={cycle} />
            ) : (
              <div className="py-8 text-center text-sm text-fg-3">No thread activity recorded.</div>
            )
          ) : (
            <Skeleton className="h-48" />
          )}
        </div>
      </Card>
    </div>
  );
}
