import { Minus, Plus, RotateCcw } from "lucide-react";
import { useEffect, useMemo, useRef, useState } from "react";

import type { LockView, Timeline } from "@/types/api";
import { SEGMENT_COLORS, fmtDuration } from "@/utils/format";

const LANE_H = 26;
const LABEL_W = 170;

const SEG_LABEL: Record<string, string> = {
  running: "running",
  holding: "running inside a critical section",
  blocked_lock: "blocked waiting for a lock",
  waiting_cond: "waiting on a condition variable",
  blocked_join: "blocked in pthread_join",
  barrier: "waiting at a barrier",
};

export function ThreadTimeline({ timeline, locks, highlight }: { timeline: Timeline; locks: Map<number, LockView>; highlight?: Set<number> }) {
  const wrap = useRef<HTMLDivElement>(null);
  const [width, setWidth] = useState(800);
  const [view, setView] = useState<[number, number]>([0, 1]);
  const [hover, setHover] = useState<{ x: number; y: number; text: string } | null>(null);
  const drag = useRef<{ x: number; v: [number, number] } | null>(null);

  useEffect(() => {
    const el = wrap.current;
    if (!el) return;
    const ro = new ResizeObserver(() => setWidth(Math.max(200, el.clientWidth - LABEL_W)));
    ro.observe(el);
    return () => ro.disconnect();
  }, []);

  const dur = timeline.duration_ns;
  const [v0, v1] = view;
  const span = (v1 - v0) * dur;
  const x = (ns: number) => ((ns / dur - v0) / (v1 - v0)) * width;

  const ticks = useMemo(() => {
    const out: number[] = [];
    const step = niceStep(span / 6);
    const start = Math.ceil((v0 * dur) / step) * step;
    for (let t = start; t <= v1 * dur; t += step) out.push(t);
    return out;
  }, [span, v0, v1, dur]);

  const zoom = (f: number) => {
    const mid = (v0 + v1) / 2;
    const half = Math.min(0.5, ((v1 - v0) / 2) * f);
    const lo = Math.max(0, Math.min(1 - 2 * half, mid - half));
    setView([lo, lo + 2 * half]);
  };

  return (
    <div ref={wrap} className="relative select-none">
      <div className="mb-2 flex items-center justify-between gap-2">
        <div className="flex flex-wrap items-center gap-3 text-xs text-fg-2">
          {Object.entries(SEGMENT_COLORS).map(([k, c]) => (
            <span key={k} className="inline-flex items-center gap-1.5">
              <span className="h-2.5 w-4 rounded-sm" style={{ background: c }} />
              {k.replace("_", " ")}
            </span>
          ))}
        </div>
        <div className="flex items-center gap-1">
          <span className="mono mr-2 text-[11px] text-fg-3">window {fmtDuration(span)}</span>
          <button className="btn p-1.5" onClick={() => zoom(0.5)} aria-label="Zoom in">
            <Plus size={13} />
          </button>
          <button className="btn p-1.5" onClick={() => zoom(2)} aria-label="Zoom out">
            <Minus size={13} />
          </button>
          <button className="btn p-1.5" onClick={() => setView([0, 1])} aria-label="Reset zoom">
            <RotateCcw size={13} />
          </button>
        </div>
      </div>
      {timeline.downsampled && (
        <div className="mb-2 text-[11px] text-fg-3">
          Lanes are down-sampled to ~{fmtDuration(timeline.resolution_ns)} buckets (blocking states are emphasised so short waits stay visible).
        </div>
      )}
      <div className="overflow-hidden rounded-lg border hairline bg-bg-elev">
        <svg width={LABEL_W + width} height={22 + timeline.lanes.length * LANE_H} className="block cursor-grab active:cursor-grabbing"
          onPointerDown={(e) => {
            drag.current = { x: e.clientX, v: view };
            (e.target as Element).setPointerCapture?.(e.pointerId);
          }}
          onPointerMove={(e) => {
            if (!drag.current) return;
            const dx = (e.clientX - drag.current.x) / width;
            const w = drag.current.v[1] - drag.current.v[0];
            const lo = Math.max(0, Math.min(1 - w, drag.current.v[0] - dx * w));
            setView([lo, lo + w]);
          }}
          onPointerUp={() => (drag.current = null)}
          onWheel={(e) => {
            if (!e.ctrlKey && !e.metaKey) return;
            zoom(e.deltaY > 0 ? 1.25 : 0.8);
          }}
          role="img"
          aria-label="Per-thread activity timeline"
        >
          {ticks.map((t) => (
            <g key={t}>
              <line x1={LABEL_W + x(t)} x2={LABEL_W + x(t)} y1={18} y2={22 + timeline.lanes.length * LANE_H} stroke="var(--line)" />
              <text x={LABEL_W + x(t) + 3} y={13} fontSize={10} fill="var(--fg-3)" className="mono">
                {fmtDuration(t, 2)}
              </text>
            </g>
          ))}
          {timeline.lanes.map((lane, i) => {
            const y = 22 + i * LANE_H;
            const hl = highlight?.has(lane.thread);
            return (
              <g key={lane.thread}>
                <rect x={0} y={y} width={LABEL_W + width} height={LANE_H} fill={i % 2 ? "transparent" : "var(--surface)"} opacity={0.5} />
                <text x={10} y={y + 17} fontSize={11.5} fill={hl ? "var(--crit)" : "var(--fg)"} className="mono">
                  <tspan fill="var(--fg-3)">T{lane.thread} </tspan>
                  {lane.label.length > 16 ? lane.label.slice(0, 15) + "…" : lane.label}
                </text>
                <svg x={LABEL_W} y={y} width={width} height={LANE_H} overflow="hidden">
                  {lane.segments.map((s, k) => {
                    const sx = x(s[0]);
                    const ex = x(s[1]);
                    if (ex < 0 || sx > width) return null;
                    const lockLabel = s[3] !== null ? locks.get(s[3])?.label : undefined;
                    return (
                      <rect
                        key={k}
                        x={Math.max(0, sx)}
                        y={6}
                        width={Math.max(1, Math.min(width, ex) - Math.max(0, sx))}
                        height={LANE_H - 12}
                        rx={2}
                        fill={SEGMENT_COLORS[s[2]] ?? "var(--fg-3)"}
                        opacity={s[2] === "running" ? 0.55 : 0.95}
                        onPointerMove={(e) =>
                          setHover({
                            x: e.clientX,
                            y: e.clientY,
                            text: `${lane.label}: ${SEG_LABEL[s[2]] ?? s[2]}${lockLabel ? ` (${lockLabel})` : ""} · ${fmtDuration(s[1] - s[0])} · from ${fmtDuration(s[0], 3)}`,
                          })
                        }
                        onPointerLeave={() => setHover(null)}
                      />
                    );
                  })}
                  {lane.end_ns !== null && x(lane.end_ns) >= 0 && x(lane.end_ns) <= width && (
                    <line x1={x(lane.end_ns)} x2={x(lane.end_ns)} y1={4} y2={LANE_H - 4} stroke="var(--fg-3)" strokeWidth={2} />
                  )}
                </svg>
              </g>
            );
          })}
        </svg>
      </div>
      {hover && (
        <div className="pointer-events-none fixed z-50 max-w-sm rounded-lg border hairline bg-surface-3 px-2.5 py-1.5 text-xs text-fg shadow-xl" style={{ left: hover.x + 12, top: hover.y + 12 }}>
          {hover.text}
        </div>
      )}
      <div className="mt-1.5 text-[11px] text-fg-3">Drag to pan · Ctrl + scroll or buttons to zoom · a vertical tick marks thread exit</div>
    </div>
  );
}

function niceStep(raw: number): number {
  if (raw <= 0) return 1;
  const p = Math.pow(10, Math.floor(Math.log10(raw)));
  const m = raw / p;
  return (m < 1.5 ? 1 : m < 3.5 ? 2 : m < 7.5 ? 5 : 10) * p;
}
