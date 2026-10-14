import { motion } from "motion/react";

import type { Finding, LockView, ThreadView } from "@/types/api";

/** Compact circular diagram of a finding's cycle (wait-for or lock-order). */
export function CycleDiagram({ finding, threads, locks, size = 220 }: { finding: Finding; threads: Map<number, ThreadView>; locks: Map<number, LockView>; size?: number }) {
  const cyc = finding.cycle;
  if (!cyc || cyc.nodes.length === 0) return null;
  const n = cyc.nodes.length;
  const cx = size / 2;
  const cy = size / 2;
  const r = n === 1 ? 0 : size / 2 - 42;
  const color = finding.classification === "confirmed_deadlock" ? "var(--crit)" : "var(--warn)";
  const pts = cyc.nodes.map((node, i) => {
    const a = (i / n) * Math.PI * 2 - Math.PI / 2;
    return { ...node, x: cx + r * Math.cos(a), y: cy + r * Math.sin(a) };
  });
  const label = (node: { kind: string; id: number }) =>
    node.kind === "thread" ? threads.get(node.id)?.label ?? `T${node.id}` : locks.get(node.id)?.label ?? `L${node.id}`;
  return (
    <svg width={size} height={size} viewBox={`0 0 ${size} ${size}`} role="img" aria-label={`Cycle ${cyc.path}`}>
      <defs>
        <marker id={`arrow-${finding.id}`} viewBox="0 0 10 10" refX="9" refY="5" markerWidth="7" markerHeight="7" orient="auto-start-reverse">
          <path d="M0,0 L10,5 L0,10 z" fill={color} />
        </marker>
      </defs>
      {n === 1 ? (
        <path d={`M ${cx + 16} ${cy - 10} C ${cx + 70} ${cy - 70}, ${cx - 70} ${cy - 70}, ${cx - 16} ${cy - 10}`} fill="none" stroke={color} strokeWidth={2.2} markerEnd={`url(#arrow-${finding.id})`} strokeDasharray="6 4" style={{ animation: "var(--animate-dash)" }} />
      ) : (
        pts.map((p, i) => {
          const q = pts[(i + 1) % n];
          const dx = q.x - p.x;
          const dy = q.y - p.y;
          const len = Math.hypot(dx, dy) || 1;
          const ux = dx / len;
          const uy = dy / len;
          // bend each edge outward so the loop reads clearly
          const mx = (p.x + q.x) / 2 - uy * 18;
          const my = (p.y + q.y) / 2 + ux * 18;
          return (
            <motion.path
              key={i}
              d={`M ${p.x + ux * 22} ${p.y + uy * 22} Q ${mx} ${my} ${q.x - ux * 24} ${q.y - uy * 24}`}
              fill="none"
              stroke={color}
              strokeWidth={2.2}
              strokeDasharray="6 4"
              markerEnd={`url(#arrow-${finding.id})`}
              initial={{ opacity: 0 }}
              animate={{ opacity: 1 }}
              transition={{ duration: 0.5, delay: i * 0.15 }}
              style={{ animation: "var(--animate-dash)" }}
            />
          );
        })
      )}
      {pts.map((p, i) => (
        <g key={i}>
          {p.kind === "thread" ? (
            <circle cx={p.x} cy={p.y} r={18} fill="var(--surface-2)" stroke={color} strokeWidth={2.2} />
          ) : (
            <rect x={p.x - 22} y={p.y - 13} width={44} height={26} rx={7} fill="var(--surface-2)" stroke={color} strokeWidth={2.2} />
          )}
          <text x={p.x} y={p.y + 4} textAnchor="middle" fontSize={11} className="mono" fill="var(--fg)">
            {p.kind === "thread" ? `T${p.id}` : `L${p.id}`}
          </text>
          <text x={p.x} y={p.y + (p.y > cy ? 34 : -26)} textAnchor="middle" fontSize={10} fill="var(--fg-2)" className="mono">
            {label(p).slice(0, 16)}
          </text>
        </g>
      ))}
    </svg>
  );
}
