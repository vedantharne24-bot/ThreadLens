import { AnimatePresence, motion, useMotionValueEvent, useScroll } from "motion/react";
import { useRef, useState } from "react";

import { cx } from "@/components/ui";

export const STEPS = [
  { title: "Thread A acquires Lock 1", text: "teller-1 enters transfer() and locks the source account.", log: "#15 LOCK_ACQUIRED  teller-1  account[A]" },
  { title: "Thread B acquires Lock 2", text: "teller-2 does the same for the opposite transfer.", log: "#22 LOCK_ACQUIRED  teller-2  account[B]" },
  { title: "Thread A requests Lock 2", text: "Lock 2 is held by B, so A blocks. The collector records LOCK_REQUEST — and no LOCK_ACQUIRED follows.", log: "#27 LOCK_REQUEST   teller-1  account[B]" },
  { title: "Thread B requests Lock 1", text: "Lock 1 is held by A, so B blocks too.", log: "#30 LOCK_REQUEST   teller-2  account[A]" },
  { title: "The circular dependency appears", text: "A waits for B (via Lock 2) and B waits for A (via Lock 1): a cycle in the wait-for graph. Neither can ever continue.", log: "wait-for: teller-1 → teller-2 → teller-1" },
  { title: "ThreadLens highlights the cycle", text: "Every edge is an observed, untimed wait; ownership comes from recorded acquisitions; no events were lost; a collector heartbeat proves the stream was drained. → confirmed.", log: "HEARTBEAT seq=31 · cycle persists · CONFIRMED" },
  { title: "The diagnosis explains the evidence", text: "Four ordered events with source lines, the cycle, the Coffman conditions, and a suggested fix: acquire both accounts in one global order.", log: "F1 confirmed_deadlock · bank_transfer.c:32,34" },
];

const A = { x: 110, y: 190 };
const B = { x: 410, y: 190 };
const L1 = { x: 260, y: 78 };
const L2 = { x: 260, y: 302 };

function Edge({ from, to, show, color, dashed, label, curve = 0 }: { from: { x: number; y: number }; to: { x: number; y: number }; show: boolean; color: string; dashed?: boolean; label?: string; curve?: number }) {
  const dx = to.x - from.x;
  const dy = to.y - from.y;
  const len = Math.hypot(dx, dy);
  const ux = dx / len;
  const uy = dy / len;
  const s = { x: from.x + ux * 34, y: from.y + uy * 30 };
  const e = { x: to.x - ux * 38, y: to.y - uy * 32 };
  const m = { x: (s.x + e.x) / 2 - uy * curve, y: (s.y + e.y) / 2 + ux * curve };
  return (
    <AnimatePresence>
      {show && (
        <motion.g initial={{ opacity: 0 }} animate={{ opacity: 1 }} exit={{ opacity: 0 }} transition={{ duration: 0.4 }}>
          <path
            d={`M ${s.x} ${s.y} Q ${m.x} ${m.y} ${e.x} ${e.y}`}
            fill="none"
            stroke={color}
            strokeWidth={2.6}
            strokeDasharray={dashed ? "7 5" : undefined}
            markerEnd="url(#story-arrow)"
            style={{ transition: "stroke 0.5s", animation: dashed ? "var(--animate-dash)" : undefined }}
          />
          {label && (
            <text x={m.x} y={m.y - 8} textAnchor="middle" fontSize={11} className="mono" fill="var(--fg-2)">
              {label}
            </text>
          )}
        </motion.g>
      )}
    </AnimatePresence>
  );
}

function Node({ p, label, sub, kind, color }: { p: { x: number; y: number }; label: string; sub: string; kind: "thread" | "lock"; color: string }) {
  return (
    <g>
      {kind === "thread" ? (
        <circle cx={p.x} cy={p.y} r={30} fill="var(--surface-2)" strokeWidth={3} stroke={color} style={{ transition: "stroke 0.5s" }} />
      ) : (
        <rect x={p.x - 40} y={p.y - 22} width={80} height={44} rx={11} fill="var(--surface-2)" strokeWidth={3} stroke={color} style={{ transition: "stroke 0.5s" }} />
      )}
      <text x={p.x} y={p.y + 4} textAnchor="middle" fontSize={13} fontWeight={600} fill="var(--fg)">
        {label}
      </text>
      <text x={p.x} y={p.y + (kind === "thread" ? 50 : 40)} textAnchor="middle" fontSize={10.5} className="mono" fill="var(--fg-3)">
        {sub}
      </text>
    </g>
  );
}

export function StoryStage({ step }: { step: number }) {
  const cyc = step >= 4;
  const red = "var(--crit)";
  return (
    <svg viewBox="0 0 520 380" className="h-full w-full" role="img" aria-label={`Deadlock formation, step ${step + 1}: ${STEPS[step].title}`}>
      <defs>
        <marker id="story-arrow" viewBox="0 0 10 10" refX="8" refY="5" markerWidth="7" markerHeight="7" orient="auto-start-reverse">
          <path d="M0,0 L10,5 L0,10 z" fill="context-stroke" />
        </marker>
        <radialGradient id="story-glow">
          <stop offset="0%" stopColor="var(--crit)" stopOpacity={0.35} />
          <stop offset="100%" stopColor="var(--crit)" stopOpacity={0} />
        </radialGradient>
      </defs>
      <AnimatePresence>
        {step >= 5 && (
          <motion.g initial={{ opacity: 0, scale: 0.85 }} animate={{ opacity: 1, scale: 1 }} exit={{ opacity: 0 }} style={{ transformOrigin: "260px 190px" }} transition={{ duration: 0.6, ease: [0.16, 1, 0.3, 1] }}>
            <circle cx={260} cy={190} r={170} fill="url(#story-glow)" />
            <circle cx={260} cy={190} r={150} fill="none" stroke={red} strokeWidth={1.5} strokeDasharray="3 6" style={{ animation: "var(--animate-dash)" }} />
            <rect x={196} y={176} width={128} height={28} rx={14} fill="color-mix(in oklab, var(--crit) 18%, var(--surface))" stroke={red} />
            <text x={260} y={195} textAnchor="middle" fontSize={12} fontWeight={700} letterSpacing="0.12em" fill={red}>
              CONFIRMED
            </text>
          </motion.g>
        )}
      </AnimatePresence>
      <Edge from={L1} to={A} show={step >= 0} color={cyc ? red : "var(--violet)"} label={step < 4 ? "held by" : undefined} curve={-14} />
      <Edge from={L2} to={B} show={step >= 1} color={cyc ? red : "var(--violet)"} label={step < 4 ? "held by" : undefined} curve={-14} />
      <Edge from={A} to={L2} show={step >= 2} color={cyc ? red : "var(--warn)"} dashed label={step < 4 ? "waits for" : undefined} curve={-14} />
      <Edge from={B} to={L1} show={step >= 3} color={cyc ? red : "var(--warn)"} dashed label={step < 4 ? "waits for" : undefined} curve={-14} />
      <Node p={A} label="A" sub="teller-1 · T1" kind="thread" color={cyc ? red : step >= 2 ? "var(--warn)" : "var(--cyan)"} />
      <Node p={B} label="B" sub="teller-2 · T2" kind="thread" color={cyc ? red : step >= 3 ? "var(--warn)" : "var(--cyan)"} />
      <Node p={L1} label="Lock 1" sub="account[A] · L1" kind="lock" color={cyc ? red : "var(--violet)"} />
      <Node p={L2} label="Lock 2" sub="account[B] · L2" kind="lock" color={cyc ? red : "var(--violet)"} />
    </svg>
  );
}

/** Sticky, scroll-driven explanation of how a deadlock forms (illustrative data). */
export function DeadlockStory() {
  const ref = useRef<HTMLDivElement>(null);
  const { scrollYProgress } = useScroll({ target: ref, offset: ["start start", "end end"] });
  const [step, setStep] = useState(0);
  useMotionValueEvent(scrollYProgress, "change", (p) => setStep(Math.min(STEPS.length - 1, Math.max(0, Math.floor(p * STEPS.length)))));
  return (
    <section ref={ref} className="relative" style={{ height: `${STEPS.length * 70}vh` }} aria-label="How a deadlock forms">
      <div className="sticky top-0 flex h-screen items-center">
        <div className="mx-auto grid w-full max-w-6xl items-center gap-10 px-6 lg:grid-cols-[1fr_1.1fr]">
          <div>
            <div className="eyebrow mb-3">How a deadlock forms</div>
            <h2 className="text-3xl font-semibold tracking-tight sm:text-4xl">Seven events. One circular wait.</h2>
            <span className="chip mt-4">illustrative sequence — run it for real with the “Bank transfer deadlock” scenario</span>
            <ol className="mt-8 space-y-1">
              {STEPS.map((s, i) => (
                <li key={i}>
                  <motion.div
                    animate={{ opacity: i === step ? 1 : i < step ? 0.55 : 0.3, x: i === step ? 6 : 0 }}
                    transition={{ duration: 0.35 }}
                    className={cx("rounded-xl border px-4 py-3", i === step ? "border-line-strong bg-surface" : "border-transparent")}
                  >
                    <div className="flex items-baseline gap-3">
                      <span className="mono text-xs" style={{ color: i >= 4 && i <= step ? "var(--crit)" : "var(--cyan)" }}>
                        0{i + 1}
                      </span>
                      <span className="font-medium">{s.title}</span>
                    </div>
                    <AnimatePresence initial={false}>
                      {i === step && (
                        <motion.p initial={{ height: 0, opacity: 0 }} animate={{ height: "auto", opacity: 1 }} exit={{ height: 0, opacity: 0 }} className="overflow-hidden pl-8 pt-1.5 text-sm leading-relaxed text-fg-2">
                          {s.text}
                        </motion.p>
                      )}
                    </AnimatePresence>
                  </motion.div>
                </li>
              ))}
            </ol>
          </div>
          <div className="card relative overflow-hidden p-4" style={{ boxShadow: step >= 4 ? "var(--glow-crit)" : "var(--glow-cyan)" }}>
            <div className="grid-bg absolute inset-0 opacity-50" />
            <div className="relative aspect-[520/380]">
              <StoryStage step={step} />
            </div>
            <div className="mono relative mt-2 h-24 overflow-hidden rounded-lg border hairline bg-bg-elev p-3 text-[11.5px]">
              <AnimatePresence initial={false}>
                {STEPS.slice(0, step + 1)
                  .slice(-4)
                  .map((s, i, arr) => (
                    <motion.div key={s.log} initial={{ opacity: 0, y: 8 }} animate={{ opacity: i === arr.length - 1 ? 1 : 0.5, y: 0 }} exit={{ opacity: 0 }} className={i === arr.length - 1 && step >= 4 ? "text-crit" : "text-fg-2"}>
                      {s.log}
                    </motion.div>
                  ))}
              </AnimatePresence>
            </div>
            <AnimatePresence>
              {step === STEPS.length - 1 && (
                <motion.div initial={{ opacity: 0, y: 24 }} animate={{ opacity: 1, y: 0 }} exit={{ opacity: 0, y: 24 }} transition={{ type: "spring", stiffness: 260, damping: 26 }} className="card absolute inset-x-6 bottom-6 bg-surface p-4">
                  <div className="text-xs font-semibold uppercase tracking-wider text-crit">Diagnosis · confirmed deadlock</div>
                  <div className="mt-1.5 text-sm text-fg-2">
                    teller-1 holds account[A] and waits for account[B]; teller-2 holds account[B] and waits for account[A]. Mutual exclusion, hold-and-wait, no preemption and circular wait all hold.
                  </div>
                  <div className="mt-2 text-sm text-ok">Fix: lock the lower account id first in transfer().</div>
                </motion.div>
              )}
            </AnimatePresence>
          </div>
        </div>
      </div>
    </section>
  );
}
