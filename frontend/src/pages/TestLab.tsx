import { motion } from "motion/react";
import { Code2, FileUp, GitCompare, Loader2, Play, Terminal, Trash2, Upload } from "lucide-react";
import { useEffect, useMemo, useRef, useState } from "react";
import { useNavigate } from "react-router-dom";

import { Card, CardHeader, Modal, PageHeader, Segmented, Skeleton, StatusDot, TiltCard, VerdictPill, cx } from "@/components/ui";
import { usePrefs } from "@/hooks/usePrefs";
import { api } from "@/services/api";
import { openSession } from "@/services/session";
import { toast } from "@/services/toast";
import type { Health, Scenario, SessionInfo } from "@/types/api";
import { diffLines, highlight } from "@/utils/code";
import { fmtMs, timeAgo } from "@/utils/format";

const CATEGORY_LABEL: Record<string, string> = {
  deadlock: "Deadlock",
  corrected: "Corrected",
  potential: "Potential",
  contention: "Contention",
  healthy: "Healthy",
  lifecycle: "Lifecycle",
  misuse: "Misuse",
  stall: "Stall",
  stress: "Stress",
};

async function waitDone(id: string, timeoutMs: number): Promise<SessionInfo> {
  const end = Date.now() + timeoutMs;
  for (;;) {
    const s = await api.session(id);
    if (!["running", "starting", "analyzing"].includes(s.status)) return s;
    if (Date.now() > end) throw new Error(`session ${id} did not finish in time`);
    await new Promise((r) => setTimeout(r, 300));
  }
}

export default function TestLab() {
  const navigate = useNavigate();
  const prefs = usePrefs((p) => ({ ring: p.ringCapacity, block: p.blockUs, kill: p.killOnDeadlock }));
  const [scenarios, setScenarios] = useState<Scenario[] | null>(null);
  const [health, setHealth] = useState<Health | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [cat, setCat] = useState("all");
  const [busy, setBusy] = useState<string | null>(null);
  const [sourceFor, setSourceFor] = useState<Scenario | null>(null);
  const [pairRun, setPairRun] = useState<{ step: string } | null>(null);
  const [recent, setRecent] = useState<SessionInfo[] | null>(null);

  const refreshRecent = () => api.sessions(12).then(setRecent).catch(() => setRecent([]));
  useEffect(() => {
    api.scenarios().then(setScenarios).catch((e) => setError(e.message));
    api.health().then(setHealth).catch(() => undefined);
    void refreshRecent();
  }, []);

  const byId = useMemo(() => new Map((scenarios ?? []).map((s) => [s.id, s])), [scenarios]);
  const cats = useMemo(() => ["all", ...new Set((scenarios ?? []).map((s) => s.category))], [scenarios]);
  const shown = (scenarios ?? []).filter((s) => cat === "all" || s.category === cat);

  const launch = async (s: Scenario, goto = true) => {
    setBusy(s.id);
    try {
      const info = await api.createSession({ scenario_id: s.id, kill_on_deadlock: prefs.kill, ring_capacity: prefs.ring, block_us: prefs.block });
      if (goto) {
        openSession(info.id);
        toast.push({ level: "info", title: `Monitoring “${s.title}”`, body: `session ${info.id}` });
        navigate(s.category === "deadlock" ? "/app/graph" : "/app");
      }
      return info;
    } catch (e) {
      toast.push({ level: "critical", title: "Could not start session", body: (e as Error).message });
      throw e;
    } finally {
      setBusy(null);
    }
  };

  const runPair = async (orig: Scenario) => {
    const fix = orig.pair ? byId.get(orig.pair) : undefined;
    if (!fix) return;
    try {
      setPairRun({ step: `Running original: ${orig.title}…` });
      const a = await launch(orig, false);
      await waitDone(a.id, (orig.timeout_s + 15) * 1000);
      setPairRun({ step: `Running corrected: ${fix.title}…` });
      const b = await launch(fix, false);
      await waitDone(b.id, (fix.timeout_s + 15) * 1000);
      setPairRun(null);
      openSession(b.id);
      navigate(`/app/replay?tab=compare&a=${a.id}&b=${b.id}`);
    } catch (e) {
      setPairRun(null);
      toast.push({ level: "critical", title: "Pair run failed", body: (e as Error).message });
    }
  };

  return (
    <div className="space-y-6">
      <PageHeader
        eyebrow="Test lab"
        title="Run real pthread programs under ThreadLens"
        description="Every scenario is an actual compiled C/C++ program in scenarios/. Launching one starts the binary with the collector preloaded; all data you see afterwards is collected from that process. “Expected” shows the ground truth used by the automated evaluation."
        actions={
          health && (
            <span className={cx("chip", health.collector_built ? "text-ok" : "text-crit")}>
              <StatusDot status={health.collector_built ? "completed" : "failed"} />
              collector {health.collector_built ? "built" : "missing"} · {health.scenarios_built}/{health.scenarios_total} programs built
            </span>
          )
        }
      />
      {error && <Card className="p-4 text-sm text-crit">{error}</Card>}
      <div className="flex flex-wrap gap-2">
        <Segmented
          value={cat}
          onChange={setCat}
          options={cats.map((c) => ({ value: c, label: c === "all" ? "All" : CATEGORY_LABEL[c] ?? c }))}
        />
      </div>
      {!scenarios ? (
        <div className="grid gap-4 md:grid-cols-2 xl:grid-cols-3">
          {Array.from({ length: 6 }, (_, i) => (
            <Skeleton key={i} className="h-60" />
          ))}
        </div>
      ) : (
        <div className="grid gap-4 md:grid-cols-2 xl:grid-cols-3">
          {shown.map((s, i) => (
            <motion.div key={s.id} initial={{ opacity: 0, y: 12 }} animate={{ opacity: 1, y: 0 }} transition={{ delay: Math.min(i, 9) * 0.04 }}>
              <TiltCard className="h-full">
                <div className="card flex h-full flex-col p-5">
                  <div className="flex items-start justify-between gap-2">
                    <div>
                      <div className="flex flex-wrap items-center gap-1.5">
                        <span className="chip">{CATEGORY_LABEL[s.category] ?? s.category}</span>
                        <span className="chip mono">{s.language}</span>
                        {!s.built && <span className="chip text-crit">not built</span>}
                      </div>
                      <h3 className="mt-2.5 font-semibold tracking-tight">{s.title}</h3>
                    </div>
                  </div>
                  <p className="mt-2 text-sm leading-relaxed text-fg-2">{s.description}</p>
                  <p className="mt-2 text-xs text-fg-3">
                    <span className="text-fg-2">Teaches:</span> {s.teaches}
                  </p>
                  <div className="mt-3 flex flex-wrap items-center gap-2 text-xs text-fg-3">
                    <span>Expected:</span>
                    <VerdictPill verdict={s.expect.verdict} />
                    <span className="mono">{s.binary} {s.args.join(" ")}</span>
                  </div>
                  <div className="mt-auto flex flex-wrap gap-2 pt-4">
                    <button className="btn btn-primary" disabled={!s.built || busy !== null || pairRun !== null} onClick={() => void launch(s)}>
                      {busy === s.id ? <Loader2 size={14} className="animate-spin" /> : <Play size={14} />} Run
                    </button>
                    <button className="btn" onClick={() => setSourceFor(s)}>
                      <Code2 size={14} /> Source{s.pair ? " & fix" : ""}
                    </button>
                    {s.pair && (s.category === "deadlock" || s.category === "potential") && byId.get(s.pair) && s.id !== "lock-order-ba" && (
                      <button className="btn" disabled={busy !== null || pairRun !== null || !s.built} onClick={() => void runPair(s)} title={`Run ${s.title}, then ${byId.get(s.pair)?.title}, then compare`}>
                        <GitCompare size={14} /> Run original + fix
                      </button>
                    )}
                  </div>
                </div>
              </TiltCard>
            </motion.div>
          ))}
        </div>
      )}

      <div className="grid gap-6 xl:grid-cols-2">
        <CustomProgram />
        <ImportTrace />
      </div>

      <Card>
        <CardHeader eyebrow="History" title="Recent sessions" actions={<button className="btn btn-ghost text-xs" onClick={() => void refreshRecent()}>Refresh</button>} />
        <div className="divide-y divide-[var(--line)]">
          {recent === null && <Skeleton className="m-4 h-24" />}
          {recent?.length === 0 && <div className="px-4 py-8 text-center text-sm text-fg-3">No sessions yet.</div>}
          {recent?.map((s) => (
            <div key={s.id} className="flex items-center gap-3 px-4 py-2.5">
              <StatusDot status={s.status} />
              <button
                className="min-w-0 flex-1 text-left"
                onClick={() => {
                  openSession(s.id);
                  navigate("/app");
                }}
              >
                <span className="block truncate text-sm hover:text-cyan">{s.label ?? s.id}</span>
                <span className="mono block text-[11px] text-fg-3">
                  {s.id} · {timeAgo(s.created_at)} · {s.event_count.toLocaleString()} events{s.wall_ms ? ` · ${fmtMs(s.wall_ms)}` : ""}
                </span>
              </button>
              <VerdictPill verdict={s.verdict} />
              <button
                className="btn btn-ghost p-1.5 text-fg-3 hover:text-crit"
                aria-label={`Delete session ${s.id}`}
                disabled={s.status === "running"}
                onClick={async () => {
                  if (!window.confirm(`Delete session ${s.id} and all its recorded events?`)) return;
                  try {
                    await api.deleteSession(s.id);
                    void refreshRecent();
                  } catch (e) {
                    toast.push({ level: "critical", title: "Delete failed", body: (e as Error).message });
                  }
                }}
              >
                <Trash2 size={14} />
              </button>
            </div>
          ))}
        </div>
      </Card>

      <SourceModal scenario={sourceFor} pair={sourceFor?.pair ? byId.get(sourceFor.pair) : undefined} onClose={() => setSourceFor(null)} />
      <Modal open={pairRun !== null} onClose={() => undefined} title="Original vs. corrected">
        <div className="flex items-center gap-3 p-6 text-sm text-fg-2">
          <Loader2 size={18} className="animate-spin text-cyan" /> {pairRun?.step}
        </div>
      </Modal>
    </div>
  );
}

function SourceModal({ scenario, pair, onClose }: { scenario: Scenario | null; pair?: Scenario; onClose: () => void }) {
  const [files, setFiles] = useState<{ name: string; path: string; content: string }[] | null>(null);
  const [pairFiles, setPairFiles] = useState<{ name: string; path: string; content: string }[] | null>(null);
  const [view, setView] = useState<"source" | "diff">("source");
  useEffect(() => {
    if (!scenario) return;
    setFiles(null);
    setPairFiles(null);
    setView("source");
    api.scenarioSource(scenario.id).then((r) => setFiles(r.files)).catch(() => setFiles([]));
    if (pair && pair.sources[0] !== scenario.sources[0]) api.scenarioSource(pair.id).then((r) => setPairFiles(r.files)).catch(() => setPairFiles([]));
  }, [scenario, pair]);
  const diff = useMemo(() => (files?.[0] && pairFiles?.[0] ? diffLines(files[0].content.split("\n"), pairFiles[0].content.split("\n")) : null), [files, pairFiles]);
  return (
    <Modal open={scenario !== null} onClose={onClose} wide title={<span className="mono">{scenario?.sources.join(", ")}</span>}>
      {diff && (
        <div className="flex items-center justify-between border-b hairline px-5 py-2.5">
          <Segmented value={view} onChange={setView} options={[{ value: "source", label: "Source" }, { value: "diff", label: `Diff vs ${pair?.binary}` }]} />
          <span className="text-xs text-fg-3">{diff.filter((d) => d.kind !== "same").length} changed lines</span>
        </div>
      )}
      {!files ? (
        <Skeleton className="m-5 h-96" />
      ) : view === "diff" && diff ? (
        <pre className="mono overflow-x-auto p-4 text-[12.5px] leading-[1.6]">
          {diff.map((d, i) => (
            <div key={i} className={cx("flex", d.kind === "add" && "bg-ok/10", d.kind === "del" && "bg-crit/10")}>
              <span className="w-10 shrink-0 select-none pr-3 text-right text-fg-3">{d.a ?? ""}</span>
              <span className="w-10 shrink-0 select-none pr-3 text-right text-fg-3">{d.b ?? ""}</span>
              <span className={cx("w-4 shrink-0 select-none", d.kind === "add" ? "text-ok" : d.kind === "del" ? "text-crit" : "text-fg-3")}>
                {d.kind === "add" ? "+" : d.kind === "del" ? "−" : " "}
              </span>
              <span className="whitespace-pre">{highlight(d.text)}</span>
            </div>
          ))}
        </pre>
      ) : (
        files.map((f) => (
          <pre key={f.path} className="mono overflow-x-auto p-4 text-[12.5px] leading-[1.6]">
            {f.content.split("\n").map((line, i) => (
              <div key={i} className="flex">
                <span className="w-10 shrink-0 select-none pr-4 text-right text-fg-3">{i + 1}</span>
                <span className="whitespace-pre">{highlight(line)}</span>
              </div>
            ))}
          </pre>
        ))
      )}
    </Modal>
  );
}

function splitArgs(s: string): string[] {
  const out: string[] = [];
  const re = /"([^"]*)"|'([^']*)'|(\S+)/g;
  let m: RegExpExecArray | null;
  while ((m = re.exec(s))) out.push(m[1] ?? m[2] ?? m[3]);
  return out;
}

function CustomProgram() {
  const navigate = useNavigate();
  const prefs = usePrefs((p) => ({ ring: p.ringCapacity, block: p.blockUs, kill: p.killOnDeadlock }));
  const [exe, setExe] = useState("");
  const [args, setArgs] = useState("");
  const [timeout, setTimeoutS] = useState("10");
  const [busy, setBusy] = useState(false);
  return (
    <Card>
      <CardHeader icon={<Terminal size={15} />} eyebrow="Your program" title="Monitor a compiled executable" />
      <form
        className="space-y-3 p-4"
        onSubmit={async (e) => {
          e.preventDefault();
          setBusy(true);
          try {
            const info = await api.createSession({ executable: exe.trim(), args: splitArgs(args), timeout_s: Number(timeout) || 10, kill_on_deadlock: prefs.kill, ring_capacity: prefs.ring, block_us: prefs.block });
            openSession(info.id);
            navigate("/app");
          } catch (err) {
            toast.push({ level: "critical", title: "Could not start program", body: (err as Error).message });
          } finally {
            setBusy(false);
          }
        }}
      >
        <label className="block">
          <span className="eyebrow mb-1 block">Executable path (Linux)</span>
          <input className="input mono" placeholder="/home/you/project/build/myprog" value={exe} onChange={(e) => setExe(e.target.value)} required />
        </label>
        <div className="grid grid-cols-[1fr_110px] gap-3">
          <label className="block">
            <span className="eyebrow mb-1 block">Arguments</span>
            <input className="input mono" placeholder="--threads 8" value={args} onChange={(e) => setArgs(e.target.value)} />
          </label>
          <label className="block">
            <span className="eyebrow mb-1 block">Timeout (s)</span>
            <input className="input mono" value={timeout} onChange={(e) => setTimeoutS(e.target.value.replace(/[^0-9.]/g, ""))} />
          </label>
        </div>
        <p className="text-xs leading-relaxed text-fg-3">
          The program must be a dynamically linked ELF file inside the build directory or a directory listed in <span className="mono">THREADLENS_ALLOWED_DIRS</span>. It runs without a shell, in a private working directory, with resource limits and a timeout.
        </p>
        <button className="btn btn-primary" disabled={busy || !exe.trim()}>
          {busy ? <Loader2 size={14} className="animate-spin" /> : <Play size={14} />} Launch
        </button>
      </form>
    </Card>
  );
}

function ImportTrace() {
  const navigate = useNavigate();
  const input = useRef<HTMLInputElement>(null);
  const [busy, setBusy] = useState(false);
  const [drag, setDrag] = useState(false);
  const upload = async (file: File) => {
    setBusy(true);
    try {
      const { id } = await api.importTrace(file);
      openSession(id);
      toast.push({ level: "success", title: "Trace imported", body: file.name });
      navigate("/app");
    } catch (e) {
      toast.push({ level: "critical", title: "Import failed", body: (e as Error).message });
    } finally {
      setBusy(false);
    }
  };
  return (
    <Card>
      <CardHeader icon={<FileUp size={15} />} eyebrow="Offline analysis" title="Import a recorded trace" />
      <div className="p-4">
        <button
          type="button"
          className={cx("flex w-full flex-col items-center justify-center gap-2 rounded-xl border border-dashed p-8 text-sm transition-colors", drag ? "border-cyan bg-cyan/5" : "border-line-strong hover:border-cyan/50")}
          onClick={() => input.current?.click()}
          onDragOver={(e) => {
            e.preventDefault();
            setDrag(true);
          }}
          onDragLeave={() => setDrag(false)}
          onDrop={(e) => {
            e.preventDefault();
            setDrag(false);
            const f = e.dataTransfer.files[0];
            if (f) void upload(f);
          }}
        >
          {busy ? <Loader2 size={20} className="animate-spin text-cyan" /> : <Upload size={20} className="text-fg-3" />}
          <span className="text-fg-2">Drop a .tlt binary trace or a raw NDJSON export</span>
          <span className="mono text-[11px] text-fg-3">THREADLENS_TRACE=run.tlt LD_PRELOAD=libthreadlens.so ./prog</span>
        </button>
        <input ref={input} type="file" accept=".tlt,.ndjson,.json,application/octet-stream" className="hidden" onChange={(e) => e.target.files?.[0] && void upload(e.target.files[0])} />
      </div>
    </Card>
  );
}
