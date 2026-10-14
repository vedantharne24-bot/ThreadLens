import { motion } from "motion/react";
import { Activity, AlertTriangle, ArrowRight, Clock, Gauge, Lock, ShieldCheck, Timer, Users } from "lucide-react";
import { useMemo } from "react";
import { Link, useNavigate } from "react-router-dom";
import { Area, AreaChart, ResponsiveContainer, Tooltip, XAxis, YAxis } from "recharts";

import { SessionGate } from "@/components/SessionGate";
import { Card, CardHeader, KV, PageHeader, SeverityBadge, StatTile, StatusDot, VerdictPill } from "@/components/ui";
import { usePrefs } from "@/hooks/usePrefs";
import { sessionStore } from "@/services/session";
import { useStore } from "@/services/store";
import { VERDICT_META, endReasonLabel, fmtDuration, fmtMs, fmtNum, fmtRel, fmtTime, shortPath, typeColor } from "@/utils/format";
import { buildGraph } from "@/utils/graph";
import { Graph2D } from "@/visualization/Graph2D";

export default function Overview() {
  return (
    <SessionGate what="the overview">
      <OverviewBody />
    </SessionGate>
  );
}

function OverviewBody() {
  const info = useStore(sessionStore, (s) => s.info);
  const snap = useStore(sessionStore, (s) => s.snapshot)!;
  const connection = useStore(sessionStore, (s) => s.connection);
  const navigate = useNavigate();
  const running = info?.status === "running" || info?.status === "starting";
  const metrics = info?.metrics;
  const verdict = snap.verdict;
  const vm = VERDICT_META[verdict.classification];
  const findings = snap.findings.filter((f) => f.status !== "filtered");
  const alerts = findings.filter((f) => f.severity !== "info");
  const model = useMemo(() => buildGraph(snap, "resource"), [snap]);
  const durationNs = snap.now_ts && snap.first_ts ? snap.now_ts - snap.first_ts : null;
  const latency = info?.detection_latency_ms ?? metrics?.detection?.deadlock?.latency_ms ?? null;

  return (
    <div className="space-y-6">
      <PageHeader
        eyebrow="Session overview"
        title={info?.label ?? info?.id ?? "Session"}
        description={
          <span className="mono text-xs">
            {shortPath(info?.executable)} {info?.args?.join(" ")}
          </span>
        }
        actions={
          <>
            <Link to="/app/graph" className="btn">
              Lock graph <ArrowRight size={14} />
            </Link>
            <Link to="/app/diagnostics" className="btn">
              Diagnostics <ArrowRight size={14} />
            </Link>
          </>
        }
      />

      <motion.div
        initial={{ opacity: 0, y: 10 }}
        animate={{ opacity: 1, y: 0 }}
        className="card relative overflow-hidden p-5"
        style={{ boxShadow: vm.tone === "crit" ? "var(--glow-crit)" : undefined }}
      >
        <div aria-hidden className="pointer-events-none absolute inset-0 opacity-[0.08]" style={{ background: `radial-gradient(600px 160px at 0% 0%, ${vm.color}, transparent)` }} />
        <div className="relative flex flex-wrap items-center justify-between gap-4">
          <div className="min-w-0">
            <div className="eyebrow mb-1.5 flex items-center gap-2">
              <StatusDot status={info?.status ?? "completed"} />
              {running ? "monitoring live" : `session ${info?.status ?? ""}`} · {endReasonLabel(info?.end_reason)}
              {info?.exit_code !== null && info?.exit_code !== undefined && ` · exit ${info.exit_code}`}
              {info?.signal ? ` · signal ${info.signal}` : ""}
            </div>
            <div className="flex flex-wrap items-center gap-3">
              <h2 className="text-xl font-semibold tracking-tight" style={{ color: vm.color }}>
                {verdict.label}
              </h2>
              <VerdictPill verdict={verdict.classification} />
            </div>
            {verdict.notes.map((n, i) => (
              <p key={i} className="mt-1.5 max-w-3xl text-sm text-fg-2">
                {n}
              </p>
            ))}
          </div>
          {alerts.length > 0 && (
            <button className="btn" onClick={() => navigate("/app/diagnostics")}>
              <AlertTriangle size={15} style={{ color: vm.color }} /> {alerts.length} finding{alerts.length > 1 ? "s" : ""}
            </button>
          )}
        </div>
      </motion.div>

      <div className="grid grid-cols-2 gap-4 lg:grid-cols-4">
        <StatTile label="Threads observed" value={snap.stats.threads_total} icon={<Users size={15} />} sub={`${snap.stats.threads_live} alive · ${snap.stats.threads_blocked} blocked`} tone={snap.threads.some((t) => t.in_cycle) ? "crit" : "default"} />
        <StatTile label="Locks known" value={snap.stats.locks_total} icon={<Lock size={15} />} sub={`${snap.stats.locks_held} held right now`} />
        <StatTile label="Waiting threads" value={snap.stats.threads_blocked + snap.stats.threads_waiting} icon={<Clock size={15} />} sub={`${snap.stats.threads_blocked} on locks/joins · ${snap.stats.threads_waiting} on condvars/barriers`} tone={snap.stats.threads_blocked > 0 ? "warn" : "default"} />
        <StatTile label="Alerts" value={alerts.length} icon={<AlertTriangle size={15} />} sub={alerts[0]?.title ?? "none"} tone={alerts.some((a) => a.severity === "critical") ? "crit" : alerts.length ? "warn" : "ok"} />
        <StatTile
          label="Event rate"
          value={metrics?.events_per_sec ?? null}
          format={(n) => `${fmtNum(Math.round(n))}/s`}
          icon={<Activity size={15} />}
          sub={running ? "live (2 s window)" : "average over the run"}
          tone="cyan"
          unavailable={metrics ? undefined : "no metrics for imported traces"}
        />
        <StatTile label="Events" value={snap.stats.events} icon={<Gauge size={15} />} sub={snap.storage?.truncated ? "storage cap reached" : `${fmtNum(metrics?.stored_events ?? snap.stats.events)} stored`} />
        <StatTile label="Monitoring duration" value={durationNs} format={(n) => fmtDuration(n)} icon={<Timer size={15} />} sub={info?.wall_ms ? `wall clock ${fmtMs(info.wall_ms)}` : "process clock (CLOCK_MONOTONIC)"} />
        <StatTile
          label="Coverage"
          value={snap.coverage.lost_events}
          format={(n) => (n === 0 ? "complete" : `${fmtNum(n)} lost`)}
          icon={<ShieldCheck size={15} />}
          tone={snap.coverage.complete ? "ok" : "warn"}
          sub={
            snap.coverage.complete
              ? `${snap.coverage.heartbeats} heartbeats · no gaps`
              : `${snap.coverage.anomalies} anomalies · ${snap.coverage.missing_events} missing`
          }
        />
      </div>

      <div className="grid gap-6 xl:grid-cols-5">
        <Card className="xl:col-span-3">
          <CardHeader
            eyebrow="Current state"
            title="Resource allocation graph"
            actions={
              <Link to="/app/graph" className="btn btn-ghost text-xs">
                Explore <ArrowRight size={13} />
              </Link>
            }
          />
          <Graph2D model={model} mode="resource" height={340} compact />
          {latency !== null && (
            <div className="border-t hairline px-4 py-2.5 text-xs text-fg-2">
              Deadlock confirmed <span className="mono text-crit">{fmtMs(latency, 1)}</span> after the cycle-forming event (measured on the shared CLOCK_MONOTONIC).
            </div>
          )}
        </Card>
        <Card className="xl:col-span-2">
          <CardHeader eyebrow="Diagnostics" title="Findings" actions={<Link to="/app/diagnostics" className="btn btn-ghost text-xs">All <ArrowRight size={13} /></Link>} />
          <div className="divide-y divide-[var(--line)]">
            {findings.length === 0 && <div className="px-4 py-10 text-center text-sm text-fg-3">No findings{running ? " so far" : ""}.</div>}
            {findings.slice(0, 6).map((f, i) => (
              <motion.button
                key={f.id}
                initial={{ opacity: 0, x: -6 }}
                animate={{ opacity: 1, x: 0 }}
                transition={{ delay: i * 0.04 }}
                className="flex w-full items-start gap-3 px-4 py-3 text-left hover:bg-surface-2"
                onClick={() => navigate(`/app/diagnostics?f=${f.id}`)}
              >
                <SeverityBadge severity={f.severity} />
                <div className="min-w-0 flex-1">
                  <div className="truncate text-sm font-medium">{f.title}</div>
                  <div className="mt-0.5 text-xs text-fg-3">
                    {f.classification_label} · {f.status} · seq {f.detected_seq}
                  </div>
                </div>
              </motion.button>
            ))}
          </div>
        </Card>
      </div>

      <div className="grid gap-6 xl:grid-cols-5">
        <RateCard running={running} />
        <Card className="xl:col-span-2">
          <CardHeader eyebrow="Session" title="Details" />
          <div className="p-4">
            <KV
              items={[
                ["Session", <span className="mono text-xs">{info?.id}</span>],
                ["Executable", <span className="mono text-xs">{info?.executable ?? "—"}</span>],
                ["Arguments", <span className="mono text-xs">{info?.args?.length ? info.args.join(" ") : "none"}</span>],
                ["PID", <span className="mono">{info?.pid ?? "—"}</span>],
                ["Started", fmtTime(info?.started_at)],
                ["Ended", running ? "running…" : fmtTime(info?.ended_at)],
                ["Collector", metrics?.collector_connected === false ? <span className="text-crit">never connected</span> : connection === "live" ? "streaming" : "finished"],
                ["Ring buffer", metrics?.ring_capacity ? `${fmtNum(metrics.ring_capacity)} slots · depth ${metrics.ring_depth ?? 0}` : "—"],
                ["glibc owner checks", `${snap.coverage.owner_checks} (${snap.coverage.owner_mismatches} benign mismatches)`],
              ]}
            />
          </div>
        </Card>
      </div>
      <RecentEvents />
    </div>
  );
}

function RateCard({ running }: { running: boolean }) {
  const rate = useStore(sessionStore, (s) => s.rate);
  const t0 = rate[0]?.t ?? 0;
  const data = rate.map((r) => ({ t: (r.t - t0) / 1000, rate: Math.round(r.rate), events: r.events }));
  return (
    <Card className="xl:col-span-3">
      <CardHeader eyebrow="Throughput" title="Event rate" />
      <div className="h-56 px-2 py-3">
        {data.length < 2 ? (
          <div className="grid h-full place-items-center px-6 text-center text-sm text-fg-3">
            {running ? "Collecting samples…" : "The rate history is recorded while a session streams live. Open a running session to see it update."}
          </div>
        ) : (
          <ResponsiveContainer width="100%" height="100%">
            <AreaChart data={data} margin={{ left: 0, right: 12, top: 8, bottom: 0 }}>
              <defs>
                <linearGradient id="rateFill" x1="0" y1="0" x2="0" y2="1">
                  <stop offset="0%" stopColor="var(--cyan)" stopOpacity={0.35} />
                  <stop offset="100%" stopColor="var(--cyan)" stopOpacity={0} />
                </linearGradient>
              </defs>
              <XAxis dataKey="t" tickFormatter={(v) => `${Number(v).toFixed(0)}s`} stroke="var(--fg-3)" fontSize={11} tickLine={false} axisLine={false} />
              <YAxis stroke="var(--fg-3)" fontSize={11} tickLine={false} axisLine={false} width={52} tickFormatter={(v) => fmtNum(Number(v))} />
              <Tooltip
                contentStyle={{ background: "var(--surface-3)", border: "1px solid var(--line-strong)", borderRadius: 10, fontSize: 12 }}
                labelFormatter={(v) => `t = ${Number(v).toFixed(1)} s`}
                formatter={(v: number) => [`${fmtNum(v)} events/s`, "rate"]}
              />
              <Area type="monotone" dataKey="rate" stroke="var(--cyan)" strokeWidth={2} fill="url(#rateFill)" isAnimationActive={false} />
            </AreaChart>
          </ResponsiveContainer>
        )}
      </div>
    </Card>
  );
}

function RecentEvents() {
  const events = useStore(sessionStore, (s) => s.events.slice(-12));
  const unit = usePrefs((p) => p.timeUnit);
  return (
    <Card>
      <CardHeader eyebrow="Event stream" title="Most recent events" actions={<Link to="/app/events" className="btn btn-ghost text-xs">Event Explorer <ArrowRight size={13} /></Link>} />
      <div className="divide-y divide-[var(--line)]">
        {events.length === 0 && <div className="px-4 py-8 text-center text-sm text-fg-3">No events yet.</div>}
        {events
          .slice()
          .reverse()
          .map((e) => (
            <div key={e.seq} className="grid grid-cols-[70px_110px_170px_1fr] items-center gap-3 px-4 py-2 text-sm max-md:grid-cols-[60px_1fr]">
              <span className="mono text-xs text-fg-3">#{e.seq}</span>
              <span className="mono text-xs text-fg-3 max-md:hidden">{fmtRel(e.rel_ns, unit)}</span>
              <span className="mono truncate text-xs max-md:hidden" style={{ color: typeColor(e.type) }}>
                {e.type}
              </span>
              <span className="truncate text-fg-2">{e.description}</span>
            </div>
          ))}
      </div>
    </Card>
  );
}
