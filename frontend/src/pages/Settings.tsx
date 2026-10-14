import { Check, Loader2 } from "lucide-react";
import { type ReactNode, useEffect, useState } from "react";

import { Card, CardHeader, PageHeader, Segmented, Skeleton, Toggle } from "@/components/ui";
import { setPrefs, usePrefs } from "@/hooks/usePrefs";
import { api } from "@/services/api";
import { toast } from "@/services/toast";
import type { AnalysisSettings, Health } from "@/types/api";

function Row({ label, hint, children }: { label: string; hint?: ReactNode; children: ReactNode }) {
  return (
    <div className="flex flex-wrap items-center justify-between gap-4 border-b hairline px-4 py-3.5 last:border-0">
      <div className="min-w-0 max-w-xl">
        <div className="text-sm font-medium">{label}</div>
        {hint && <div className="mt-0.5 text-xs leading-relaxed text-fg-3">{hint}</div>}
      </div>
      <div className="shrink-0">{children}</div>
    </div>
  );
}

export default function SettingsPage() {
  const prefs = usePrefs((p) => p);
  const [server, setServer] = useState<{ analysis: AnalysisSettings; retention: { max_sessions: number } } | null>(null);
  const [health, setHealth] = useState<Health | null>(null);
  const [saving, setSaving] = useState(false);
  useEffect(() => {
    api.settings().then(setServer).catch(() => undefined);
    api.health().then(setHealth).catch(() => undefined);
  }, []);
  const setA = (patch: Partial<AnalysisSettings>) => server && setServer({ ...server, analysis: { ...server.analysis, ...patch } });

  return (
    <div className="space-y-6">
      <PageHeader eyebrow="Settings" title="Preferences and analysis options" description="Display preferences are stored in this browser. Analysis thresholds are stored by the backend and apply to sessions started afterwards." />
      <div className="grid gap-6 xl:grid-cols-2">
        <Card>
          <CardHeader eyebrow="Display" title="Appearance and motion" />
          <Row label="Theme">
            <Segmented value={prefs.theme} onChange={(v) => setPrefs({ theme: v })} options={[{ value: "dark", label: "Dark" }, { value: "light", label: "Light" }]} />
          </Row>
          <Row label="Motion and effects" hint="“System” follows prefers-reduced-motion. Reduced turns off animation, parallax, auto-rotation and the 3D landing scene.">
            <Segmented value={prefs.effects} onChange={(v) => setPrefs({ effects: v })} options={[{ value: "system", label: "System" }, { value: "full", label: "Full" }, { value: "reduced", label: "Reduced" }]} />
          </Row>
          <Row label="Default graph view" hint="The 2D view is equivalent and lighter; 3D needs WebGL.">
            <Segmented value={prefs.graphMode} onChange={(v) => setPrefs({ graphMode: v })} options={[{ value: "2d", label: "2D" }, { value: "3d", label: "3D" }]} />
          </Row>
          <Row label="Event time unit">
            <Segmented value={prefs.timeUnit} onChange={(v) => setPrefs({ timeUnit: v })} options={[{ value: "ms", label: "ms" }, { value: "us", label: "µs" }, { value: "ns", label: "ns" }]} />
          </Row>
          <Row label="Show filtered / informational findings" hint="Lock-order cycles removed by the single-thread or gate-lock filters, robust-mutex recoveries, …">
            <Toggle checked={prefs.showFilteredFindings} onChange={(v) => setPrefs({ showFilteredFindings: v })} label="Show filtered findings" />
          </Row>
        </Card>

        <Card>
          <CardHeader eyebrow="Collector" title="Options for new sessions" />
          <Row label="Ring buffer capacity" hint="Records buffered inside the target process (power of two). Larger rings absorb bursts at the cost of memory (128 bytes per slot).">
            <select className="input w-36" value={prefs.ringCapacity} onChange={(e) => setPrefs({ ringCapacity: Number(e.target.value) })}>
              {[1024, 4096, 16384, 65536, 262144, 1048576].map((n) => (
                <option key={n} value={n}>{n.toLocaleString()}</option>
              ))}
            </select>
          </Row>
          <Row label="Back-pressure budget" hint="How long a target thread may wait for ring space before the event is dropped (and counted as lost). Larger = complete traces but the program slows to the analysis rate; 0 = never slow the program.">
            <select className="input w-36" value={prefs.blockUs} onChange={(e) => setPrefs({ blockUs: Number(e.target.value) })}>
              {[0, 500, 2000, 10000, 100000, 500000].map((n) => (
                <option key={n} value={n}>{n === 0 ? "drop immediately" : `${n.toLocaleString()} µs`}</option>
              ))}
            </select>
          </Row>
          <Row label="Terminate after a confirmed deadlock or stall" hint="The supervisor kills the isolated test process group ~1 s after confirmation. Individual deadlocked threads cannot be unwound safely.">
            <Toggle checked={prefs.killOnDeadlock} onChange={(v) => setPrefs({ killOnDeadlock: v })} label="Kill on deadlock" />
          </Row>
        </Card>

        <Card>
          <CardHeader eyebrow="Analysis" title="Detection thresholds (backend)" />
          {!server ? (
            <Skeleton className="m-4 h-48" />
          ) : (
            <>
              <Row label="Contention: minimum acquisitions" hint="A lock is judged only after this many acquisitions.">
                <input className="input mono w-28" type="number" min={1} value={server.analysis.contention_min_acquisitions} onChange={(e) => setA({ contention_min_acquisitions: Number(e.target.value) })} />
              </Row>
              <Row label="Contention: contended ratio" hint="Fraction of acquisitions that found the lock held by another thread.">
                <input className="input mono w-28" type="number" step={0.05} min={0} max={1} value={server.analysis.contention_ratio} onChange={(e) => setA({ contention_ratio: Number(e.target.value) })} />
              </Row>
              <Row label="Contention: total waiting (ms)">
                <input className="input mono w-28" type="number" min={0} value={server.analysis.contention_total_wait_ms} onChange={(e) => setA({ contention_total_wait_ms: Number(e.target.value) })} />
              </Row>
              <Row label="Long single wait (ms)" hint="A thread waiting this long for a lock (outside any cycle) is reported as excessive waiting.">
                <input className="input mono w-28" type="number" min={1} value={server.analysis.long_wait_ms} onChange={(e) => setA({ long_wait_ms: Number(e.target.value) })} />
              </Row>
              <Row label="Stall detection (ms)" hint="All live threads blocked and no events for this long ⇒ stall (inconclusive cause).">
                <input className="input mono w-28" type="number" min={50} value={server.analysis.stall_ms} onChange={(e) => setA({ stall_ms: Number(e.target.value) })} />
              </Row>
              <Row label="Require heartbeat to confirm deadlocks" hint="Recommended. Confirmation waits until the collector proves the event stream was drained after the cycle formed.">
                <Toggle checked={server.analysis.confirm_requires_heartbeat} onChange={(v) => setA({ confirm_requires_heartbeat: v })} label="Require heartbeat" />
              </Row>
              <Row label="Retention: keep at most N sessions">
                <input className="input mono w-28" type="number" min={1} value={server.retention.max_sessions} onChange={(e) => setServer({ ...server, retention: { max_sessions: Number(e.target.value) } })} />
              </Row>
              <div className="flex justify-end p-4">
                <button
                  className="btn btn-primary"
                  disabled={saving}
                  onClick={async () => {
                    setSaving(true);
                    try {
                      setServer(await api.saveSettings(server));
                      toast.push({ level: "success", title: "Analysis settings saved" });
                    } catch (e) {
                      toast.push({ level: "critical", title: "Could not save", body: (e as Error).message });
                    } finally {
                      setSaving(false);
                    }
                  }}
                >
                  {saving ? <Loader2 size={14} className="animate-spin" /> : <Check size={14} />} Save
                </button>
              </div>
            </>
          )}
        </Card>

        <Card>
          <CardHeader eyebrow="About" title="Backend and supported primitives" />
          {!health ? (
            <div className="p-4 text-sm text-fg-3">Backend not reachable.</div>
          ) : (
            <div className="space-y-4 p-4 text-sm">
              <div className="mono text-xs text-fg-2">
                ThreadLens {health.version} · Python {health.python}
                <br />
                {health.platform}
                <br />
                collector: {health.collector_path} ({health.collector_built ? "built" : "missing"})
              </div>
              <div>
                <div className="eyebrow mb-1.5">Intercepted</div>
                <ul className="space-y-1 text-fg-2">{health.supported_primitives.map((p) => <li key={p}>• {p}</li>)}</ul>
              </div>
              <div>
                <div className="eyebrow mb-1.5">Not supported (documented limitations)</div>
                <ul className="space-y-1 text-fg-3">{health.unsupported_primitives.map((p) => <li key={p}>• {p}</li>)}</ul>
              </div>
            </div>
          )}
        </Card>
      </div>
    </div>
  );
}
