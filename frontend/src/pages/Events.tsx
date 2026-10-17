import { useVirtualizer } from "@tanstack/react-virtual";
import { ChevronLeft, ChevronRight, Download, Filter, Search, X } from "lucide-react";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { useSearchParams } from "react-router-dom";

import { SessionGate } from "@/components/SessionGate";
import { Card, Drawer, KV, LockChip, PageHeader, Skeleton, ThreadChip, Toggle, cx } from "@/components/ui";
import { usePrefs } from "@/hooks/usePrefs";
import { type EventQuery, api } from "@/services/api";
import { sessionStore } from "@/services/session";
import { useStore } from "@/services/store";
import type { EventView } from "@/types/api";
import { TYPE_GROUPS, fmtNum, fmtRel, typeColor } from "@/utils/format";

export default function Events() {
  return (
    <SessionGate what="the event explorer">
      <EventsBody />
    </SessionGate>
  );
}

const ROW_H = 38;

function EventsBody() {
  const sid = useStore(sessionStore, (s) => s.activeId)!;
  const snap = useStore(sessionStore, (s) => s.snapshot)!;
  const running = useStore(sessionStore, (s) => s.info?.status === "running");
  const unit = usePrefs((p) => p.timeUnit);
  const [params, setParams] = useSearchParams();

  const [types, setTypes] = useState<Set<string>>(() => new Set(params.get("types")?.split(",").filter(Boolean) ?? []));
  const [thread, setThread] = useState(params.get("threads") ?? "");
  const [lock, setLock] = useState(params.get("locks") ?? "");
  const [q, setQ] = useState(params.get("q") ?? "");
  const [qDebounced, setQDebounced] = useState(params.get("q") ?? "");
  const [fromMs, setFromMs] = useState("");
  const [toMs, setToMs] = useState("");
  const [follow, setFollow] = useState(true);
  const [rows, setRows] = useState<EventView[]>([]);
  const [total, setTotal] = useState<number | null>(null);
  const [hasMore, setHasMore] = useState(true);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [openSeq, setOpenSeq] = useState<number | null>(params.get("seq") ? Number(params.get("seq")) : null);
  const loadingRef = useRef(false);

  useEffect(() => {
    const t = window.setTimeout(() => setQDebounced(q), 300);
    return () => window.clearTimeout(t);
  }, [q]);

  const query: EventQuery = useMemo(
    () => ({
      types: [...types].join(",") || undefined,
      threads: thread || undefined,
      locks: lock || undefined,
      q: qDebounced || undefined,
      from_ns: fromMs ? Math.round(Number(fromMs) * 1e6) : undefined,
      to_ns: toMs ? Math.round(Number(toMs) * 1e6) : undefined,
    }),
    [types, thread, lock, qDebounced, fromMs, toMs],
  );

  const loadPage = useCallback(
    async (reset: boolean, after?: number) => {
      if (loadingRef.current) return;
      loadingRef.current = true;
      setLoading(true);
      setError(null);
      try {
        const page = await api.events(sid, { ...query, after_seq: after, limit: 400 });
        setRows((prev) => (reset ? page.events : [...prev, ...page.events.filter((e) => !prev.length || e.seq > prev[prev.length - 1].seq)]));
        if (page.total !== null) setTotal(page.total);
        else if (reset) setTotal(null);
        setHasMore(page.has_more);
      } catch (e) {
        setError((e as Error).message);
      } finally {
        loadingRef.current = false;
        setLoading(false);
      }
    },
    [sid, query],
  );

  // reset on filter change
  useEffect(() => {
    setRows([]);
    setHasMore(true);
    void loadPage(true);
    const p: Record<string, string> = {};
    if (thread) p.threads = thread;
    if (lock) p.locks = lock;
    if (types.size) p.types = [...types].join(",");
    setParams(p, { replace: true });
  }, [loadPage]); // eslint-disable-line react-hooks/exhaustive-deps

  // live follow: once we have reached the end of the list, poll for newer events
  const rowsRef = useRef(rows);
  rowsRef.current = rows;
  const hasMoreRef = useRef(hasMore);
  hasMoreRef.current = hasMore;
  useEffect(() => {
    if (!running || !follow) return;
    const timer = window.setInterval(() => {
      const prev = rowsRef.current;
      if (prev.length === 0) void loadPage(true);
      else if (!hasMoreRef.current) void loadPage(false, prev[prev.length - 1].seq);
    }, 1200);
    return () => window.clearInterval(timer);
  }, [running, follow, loadPage]);

  const scroller = useRef<HTMLDivElement>(null);
  const virt = useVirtualizer({ count: rows.length, getScrollElement: () => scroller.current, estimateSize: () => ROW_H, overscan: 16 });
  const items = virt.getVirtualItems();
  useEffect(() => {
    const last = items[items.length - 1];
    if (last && last.index >= rows.length - 30 && hasMore && !loading && rows.length) void loadPage(false, rows[rows.length - 1].seq);
  }, [items, rows, hasMore, loading, loadPage]);

  const toggleType = (t: string) =>
    setTypes((s) => {
      const n = new Set(s);
      if (n.has(t)) n.delete(t);
      else n.add(t);
      return n;
    });
  const toggleGroup = (g: string) =>
    setTypes((s) => {
      const all = TYPE_GROUPS[g];
      const n = new Set(s);
      const on = all.every((t) => n.has(t));
      all.forEach((t) => (on ? n.delete(t) : n.add(t)));
      return n;
    });
  const clear = () => {
    setTypes(new Set());
    setThread("");
    setLock("");
    setQ("");
    setFromMs("");
    setToMs("");
  };

  return (
    <div className="space-y-5">
      <PageHeader
        eyebrow="Event explorer"
        title="Recorded synchronization events"
        description="Every row is one record produced by the collector inside the target process. Sequence numbers are the collector's total order; timestamps are CLOCK_MONOTONIC in the target."
        actions={<ExportMenu sid={sid} />}
      />
      <Card className="p-4">
        <div className="flex flex-wrap items-center gap-3">
          <div className="relative min-w-56 flex-1">
            <Search size={15} className="absolute left-3 top-1/2 -translate-y-1/2 text-fg-3" />
            <input className="input pl-9" placeholder="Search descriptions (e.g. account[B], EDEADLK)…" value={q} onChange={(e) => setQ(e.target.value)} aria-label="Search events" />
          </div>
          <select className="input w-auto" value={thread} onChange={(e) => setThread(e.target.value)} aria-label="Filter by thread">
            <option value="">All threads</option>
            {snap.threads.map((t) => (
              <option key={t.index} value={t.index}>
                T{t.index} · {t.label}
              </option>
            ))}
          </select>
          <select className="input w-auto" value={lock} onChange={(e) => setLock(e.target.value)} aria-label="Filter by lock">
            <option value="">All locks</option>
            {snap.locks.map((l) => (
              <option key={l.id} value={l.id}>
                L{l.id} · {l.label}
              </option>
            ))}
          </select>
          <div className="flex items-center gap-1.5 text-xs text-fg-3">
            <input className="input w-24" placeholder="from ms" value={fromMs} onChange={(e) => setFromMs(e.target.value.replace(/[^0-9.]/g, ""))} aria-label="From time (ms)" />
            –
            <input className="input w-24" placeholder="to ms" value={toMs} onChange={(e) => setToMs(e.target.value.replace(/[^0-9.]/g, ""))} aria-label="To time (ms)" />
          </div>
          {running && (
            <label className="flex items-center gap-2 text-xs text-fg-2">
              <Toggle checked={follow} onChange={setFollow} label="Follow live events" /> follow live
            </label>
          )}
          <button className="btn btn-ghost text-xs" onClick={clear}>
            <X size={13} /> Clear
          </button>
        </div>
        <div className="mt-3 flex flex-wrap gap-x-5 gap-y-2">
          {Object.entries(TYPE_GROUPS).map(([g, list]) => (
            <div key={g} className="flex flex-wrap items-center gap-1.5">
              <button className="eyebrow mr-1 hover:text-fg" onClick={() => toggleGroup(g)} title={`Toggle all ${g}`}>
                <Filter size={10} className="mr-1 inline" />
                {g}
              </button>
              {list.map((t) => (
                <button
                  key={t}
                  onClick={() => toggleType(t)}
                  className={cx("chip mono text-[10.5px] transition-colors", types.has(t) && "text-fg")}
                  style={types.has(t) ? { borderColor: typeColor(t), background: `color-mix(in oklab, ${typeColor(t)} 14%, var(--surface-2))` } : undefined}
                  aria-pressed={types.has(t)}
                >
                  {t.replace(/_/g, " ").toLowerCase()}
                </button>
              ))}
            </div>
          ))}
        </div>
      </Card>

      <Card className="overflow-hidden">
        <div className="flex items-center justify-between border-b hairline px-4 py-2 text-xs text-fg-3">
          <span>
            {total !== null ? `${fmtNum(total)} matching events` : `${fmtNum(rows.length)} loaded`}
            {total !== null && rows.length < total && ` · ${fmtNum(rows.length)} loaded`}
          </span>
          {loading && <span className="text-cyan">loading…</span>}
          {error && <span className="text-crit">{error}</span>}
        </div>
        <div className="mono grid grid-cols-[72px_110px_150px_168px_150px_1fr] gap-3 border-b hairline px-4 py-2 text-[11px] uppercase tracking-wider text-fg-3 max-lg:hidden">
          <span>seq</span>
          <span>time</span>
          <span>thread</span>
          <span>type</span>
          <span>lock</span>
          <span>description</span>
        </div>
        <div ref={scroller} className="h-[60vh] overflow-y-auto" role="list" aria-label="Events">
          {rows.length === 0 && !loading && <div className="py-16 text-center text-sm text-fg-3">No events match these filters.</div>}
          {rows.length === 0 && loading && (
            <div className="space-y-2 p-4">
              {Array.from({ length: 8 }, (_, i) => (
                <Skeleton key={i} className="h-7" />
              ))}
            </div>
          )}
          <div style={{ height: virt.getTotalSize(), position: "relative" }}>
            {items.map((vi) => {
              const e = rows[vi.index];
              return (
                <button
                  key={e.seq}
                  role="listitem"
                  onClick={() => setOpenSeq(e.seq)}
                  className={cx(
                    "absolute left-0 grid w-full grid-cols-[72px_110px_150px_168px_150px_1fr] items-center gap-3 px-4 text-left text-sm hover:bg-surface-2 max-lg:grid-cols-[64px_1fr]",
                    openSeq === e.seq && "bg-surface-2",
                  )}
                  style={{ top: vi.start, height: ROW_H }}
                >
                  <span className="mono text-xs text-fg-3">#{e.seq}</span>
                  <span className="mono text-xs text-fg-3 max-lg:hidden">{fmtRel(e.rel_ns, unit)}</span>
                  <span className="truncate max-lg:hidden">{e.thread !== null ? <span className="mono text-xs"><span className="text-fg-3">T{e.thread}</span> {e.thread_label}</span> : <span className="text-fg-3">—</span>}</span>
                  <span className="mono truncate text-xs max-lg:hidden" style={{ color: typeColor(e.type) }}>
                    {e.type}
                  </span>
                  <span className="mono truncate text-xs text-violet max-lg:hidden">{e.lock_label ?? ""}</span>
                  <span className="truncate text-fg-2">
                    {e.description}
                    {e.flags.length > 0 && <span className="ml-2 text-[10px] uppercase text-fg-3">{e.flags.join(" ")}</span>}
                  </span>
                </button>
              );
            })}
          </div>
        </div>
      </Card>
      <EventDrawer sid={sid} seq={openSeq} onClose={() => setOpenSeq(null)} onNavigate={setOpenSeq} />
    </div>
  );
}

function ExportMenu({ sid }: { sid: string }) {
  const [open, setOpen] = useState(false);
  const formats: [Parameters<typeof api.exportUrl>[1], string, string][] = [
    ["csv", "CSV", "spreadsheet of all events"],
    ["json", "JSON", "array of enriched events"],
    ["ndjson", "NDJSON", "one enriched event per line"],
    ["raw", "Raw trace (NDJSON)", "re-importable into ThreadLens"],
    ["report", "Diagnostic report", "verdict, findings, graphs, coverage"],
  ];
  return (
    <div className="relative">
      <button className="btn" onClick={() => setOpen((o) => !o)} aria-expanded={open}>
        <Download size={14} /> Export
      </button>
      {open && (
        <div className="card absolute right-0 top-full z-20 mt-2 w-72 bg-surface p-1.5" onMouseLeave={() => setOpen(false)}>
          {formats.map(([f, label, desc]) => (
            <a key={f} href={api.exportUrl(sid, f)} className="block rounded-lg px-3 py-2 hover:bg-surface-2" download onClick={() => setOpen(false)}>
              <div className="text-sm">{label}</div>
              <div className="text-xs text-fg-3">{desc}</div>
            </a>
          ))}
        </div>
      )}
    </div>
  );
}

function EventDrawer({ sid, seq, onClose, onNavigate }: { sid: string; seq: number | null; onClose: () => void; onNavigate: (s: number) => void }) {
  const [data, setData] = useState<Awaited<ReturnType<typeof api.event>> | null>(null);
  const [error, setError] = useState<string | null>(null);
  useEffect(() => {
    if (seq === null) return;
    setData(null);
    setError(null);
    api.event(sid, seq).then(setData).catch((e) => setError(e.message));
  }, [sid, seq]);
  const e = data?.event;
  return (
    <Drawer open={seq !== null} onClose={onClose} title={<span className="mono">event #{seq}</span>} width={520}>
      {error && <div className="p-5 text-sm text-crit">{error}</div>}
      {!data && !error && (
        <div className="space-y-3 p-5">
          <Skeleton className="h-6 w-2/3" />
          <Skeleton className="h-40" />
        </div>
      )}
      {e && data && (
        <div className="space-y-5 p-5">
          <div>
            <div className="mono text-xs" style={{ color: typeColor(e.type) }}>
              {e.type}
            </div>
            <div className="mt-1 text-base font-medium">{e.description}</div>
          </div>
          <KV
            items={[
              ["Sequence", <span className="mono">{e.seq}</span>],
              ["Time", <span className="mono">{fmtRel(e.rel_ns, "ms")} <span className="text-fg-3">({e.ts.toLocaleString()} ns monotonic)</span></span>],
              ["Thread", e.thread !== null ? <ThreadChip index={e.thread} label={e.thread_label ?? undefined} /> : `tid ${e.tid}`],
              ["Kernel TID", <span className="mono">{e.tid}</span>],
              ["Lock", e.lock !== null ? <LockChip id={e.lock} label={e.lock_label ?? undefined} /> : "—"],
              ["Object", <span className="mono text-xs">{e.obj ?? "—"}</span>],
              ["Result", e.result_name ?? "—"],
              ["Flags", e.flags.length ? e.flags.join(", ") : "none"],
              ["Correlation", e.correlation !== null ? <button className="mono text-cyan hover:underline" onClick={() => onNavigate(e.correlation!)}>#{e.correlation}</button> : "—"],
              ["Caller", <span className="mono text-xs">{e.caller ?? "—"}</span>],
              [
                "Source",
                data.site?.function ? (
                  <span className="mono text-xs">
                    {data.site.function}
                    {data.site.file && ` — ${data.site.file}:${data.site.line ?? "?"}`}
                  </span>
                ) : (
                  <span className="text-fg-3">not symbolized (outside the main executable, or no debug info)</span>
                ),
              ],
            ]}
          />
          {Object.keys(e.metadata).length > 0 && (
            <div>
              <div className="eyebrow mb-2">Metadata</div>
              <pre className="card-raised overflow-x-auto p-3 text-xs text-fg-2">{JSON.stringify(e.metadata, null, 2)}</pre>
            </div>
          )}
          {data.related.length > 0 && (
            <div>
              <div className="eyebrow mb-2">Related</div>
              {data.related.map((r) => (
                <button key={r.seq} className="card-raised mb-1.5 w-full p-2.5 text-left text-sm hover:border-line-strong" onClick={() => onNavigate(r.seq)}>
                  <span className="mono text-xs text-fg-3">#{r.seq} · {r.relation}</span>
                  <div className="text-fg-2">{r.description}</div>
                </button>
              ))}
            </div>
          )}
          <div className="flex items-center justify-between gap-2 border-t hairline pt-4">
            <button className="btn" disabled={!data.thread_prev} onClick={() => data.thread_prev && onNavigate(data.thread_prev.seq)}>
              <ChevronLeft size={14} /> Previous in thread
            </button>
            <button className="btn" disabled={!data.thread_next} onClick={() => data.thread_next && onNavigate(data.thread_next.seq)}>
              Next in thread <ChevronRight size={14} />
            </button>
          </div>
        </div>
      )}
    </Drawer>
  );
}
