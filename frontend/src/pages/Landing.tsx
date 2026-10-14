import { motion, useMotionValueEvent, useScroll, useTransform } from "motion/react";
import { ArrowRight, Binary, Cpu, Eye, GitCompare, Network, Radar, ShieldCheck, Sparkles as SparkIcon } from "lucide-react";
import { type ReactNode, Suspense, lazy, useEffect, useMemo, useRef, useState } from "react";
import { Link } from "react-router-dom";

import { Logo } from "@/components/Logo";
import { TiltCard, cx } from "@/components/ui";
import { hasWebGL, useReducedEffects } from "@/hooks/usePrefs";
import { api } from "@/services/api";
import type { Health } from "@/types/api";
import { DeadlockStory } from "@/visualization/DeadlockStory";

const HeroScene = lazy(() => import("@/visualization/HeroScene"));

function Reveal({ children, delay = 0, className, y = 24 }: { children: ReactNode; delay?: number; className?: string; y?: number }) {
  return (
    <motion.div
      className={className}
      initial={{ opacity: 0, y, filter: "blur(6px)" }}
      whileInView={{ opacity: 1, y: 0, filter: "blur(0px)" }}
      viewport={{ once: true, margin: "-80px" }}
      transition={{ duration: 0.8, delay, ease: [0.16, 1, 0.3, 1] }}
    >
      {children}
    </motion.div>
  );
}

export default function Landing() {
  const reduced = useReducedEffects();
  const webgl = useMemo(() => hasWebGL(), []);
  const [health, setHealth] = useState<Health | null | "offline">(null);
  useEffect(() => {
    api.health().then(setHealth).catch(() => setHealth("offline"));
  }, []);

  const hero = useRef<HTMLDivElement>(null);
  const { scrollYProgress } = useScroll({ target: hero, offset: ["start start", "end start"] });
  const progress = useRef(0);
  const [heroActive, setHeroActive] = useState(true);
  useMotionValueEvent(scrollYProgress, "change", (p) => {
    progress.current = p;
    setHeroActive(p < 0.98);
  });
  const heroOpacity = useTransform(scrollYProgress, [0, 0.75], [1, 0]);
  const heroY = useTransform(scrollYProgress, [0, 1], [0, 120]);
  const show3D = webgl && !reduced;

  return (
    <div className="min-h-full overflow-x-hidden bg-bg text-fg">
      <header className="fixed inset-x-0 top-0 z-30 border-b border-transparent backdrop-blur-md [background:linear-gradient(to_bottom,color-mix(in_oklab,var(--bg)_85%,transparent),transparent)]">
        <div className="mx-auto flex h-16 max-w-7xl items-center gap-6 px-6">
          <Link to="/" className="flex items-center gap-2.5">
            <Logo size={28} />
            <span className="text-[15px] font-semibold tracking-tight">
              Thread<span className="text-cyan">Lens</span>
            </span>
          </Link>
          <nav className="hidden items-center gap-6 text-sm text-fg-2 md:flex">
            <a href="#how" className="hover:text-fg">How it works</a>
            <a href="#story" className="hover:text-fg">Deadlock story</a>
            <a href="#compare" className="hover:text-fg">Fix & compare</a>
            <a href="#scope" className="hover:text-fg">Scope</a>
          </nav>
          <Link to="/app" className="btn btn-primary ml-auto">
            Open the debugger <ArrowRight size={15} />
          </Link>
        </div>
      </header>

      {/* ------------------------------------------------------------ hero */}
      <section ref={hero} className="relative h-[100svh] min-h-[640px]">
        <div className="absolute inset-0">
          {show3D ? (
            <Suspense fallback={<StaticHero />}>
              <HeroScene progress={progress} active={heroActive} />
            </Suspense>
          ) : (
            <StaticHero />
          )}
        </div>
        <div className="pointer-events-none absolute inset-0 [background:radial-gradient(80%_60%_at_50%_35%,transparent,var(--bg))]" />
        <div className="pointer-events-none absolute inset-x-0 bottom-0 h-[62%] [background:linear-gradient(to_top,var(--bg)_35%,transparent)]" />
        <motion.div style={{ opacity: heroOpacity, y: heroY }} className="relative z-10 mx-auto flex h-full max-w-7xl flex-col justify-end px-6 pb-16">
          <motion.div initial={{ opacity: 0, y: 18 }} animate={{ opacity: 1, y: 0 }} transition={{ duration: 0.9, delay: 0.2, ease: [0.16, 1, 0.3, 1] }}>
            <div className="eyebrow mb-4 flex flex-wrap items-center gap-2">
              <span className="chip">Linux · glibc · POSIX threads</span>
              <span className="chip">LD_PRELOAD collector</span>
              <HealthChip health={health} />
            </div>
            <h1 className="max-w-4xl text-4xl font-semibold leading-[1.05] tracking-tight sm:text-6xl">
              See <span className="text-gradient">who waits for whom</span> — and the evidence that proves it.
            </h1>
            <p className="mt-5 max-w-2xl text-base leading-relaxed text-fg-2 sm:text-lg">
              ThreadLens is a live concurrency debugger for pthread programs. It records every lock request, acquisition and release from the running process, rebuilds wait-for and lock-order graphs, and explains deadlocks with the exact events behind them.
            </p>
            <div className="pointer-events-auto mt-8 flex flex-wrap gap-3">
              <Link to="/app/lab" className="btn btn-primary px-5 py-2.5 text-base">
                Run a real deadlock <ArrowRight size={17} />
              </Link>
              <a href="#story" className="btn px-5 py-2.5 text-base">
                Watch one form
              </a>
            </div>
          </motion.div>
        </motion.div>
        <motion.div className="absolute bottom-6 left-1/2 z-10 -translate-x-1/2 text-fg-3" animate={reduced ? undefined : { y: [0, 6, 0] }} transition={{ duration: 2, repeat: Infinity }}>
          <span className="mono text-[11px] tracking-widest">SCROLL</span>
        </motion.div>
      </section>

      {/* ---------------------------------------------------- how it works */}
      <section id="how" className="relative mx-auto max-w-7xl px-6 py-28">
        <Reveal>
          <div className="eyebrow mb-3">How it works</div>
          <h2 className="max-w-3xl text-3xl font-semibold tracking-tight sm:text-4xl">From a pthread call to an explained deadlock in milliseconds.</h2>
          <p className="mt-4 max-w-2xl text-fg-2">No recompilation and no source changes: the collector interposes on the program's calls into glibc. Every step below runs on real Linux.</p>
        </Reveal>
        <Pipeline />
      </section>

      {/* ------------------------------------------------- deadlock story */}
      <div id="story">
        <DeadlockStory />
      </div>

      {/* ---------------------------------------------- original vs fixed */}
      <section id="compare" className="mx-auto max-w-7xl px-6 py-28">
        <Reveal>
          <div className="eyebrow mb-3">Fix and compare</div>
          <h2 className="max-w-3xl text-3xl font-semibold tracking-tight sm:text-4xl">Change one line. Run it again. Compare what actually happened.</h2>
          <p className="mt-4 max-w-2xl text-fg-2">
            The corrected program locks the lower account id first. ThreadLens runs both versions and compares completion, findings, event counts, contention and time — while reminding you that one clean run is evidence, not proof.
          </p>
        </Reveal>
        <div className="mt-12 grid gap-6 lg:grid-cols-2">
          <Reveal delay={0.05}>
            <ComparePanel kind="original" />
          </Reveal>
          <Reveal delay={0.15}>
            <ComparePanel kind="fixed" />
          </Reveal>
        </div>
      </section>

      {/* ------------------------------------------------- classifications */}
      <section className="mx-auto max-w-7xl px-6 pb-28">
        <Reveal>
          <div className="eyebrow mb-3">Conservative by design</div>
          <h2 className="max-w-3xl text-3xl font-semibold tracking-tight sm:text-4xl">Not every slow lock is a deadlock.</h2>
        </Reveal>
        <div className="mt-10 grid gap-4 sm:grid-cols-2 lg:grid-cols-5">
          {[
            { c: "var(--crit)", t: "Confirmed deadlock", d: "A current wait-for cycle of observed, untimed waits with complete coverage that outlives a heartbeat." },
            { c: "var(--warn)", t: "Potential deadlock", d: "Locks acquired in conflicting orders (lock-order graph cycle). This run may have finished fine." },
            { c: "var(--warn)", t: "Contention", d: "Threads waited a lot for a lock — a performance problem, never reported as a deadlock." },
            { c: "var(--crit)", t: "Stall", d: "Everything is blocked but there is no lock cycle (e.g. a lost condition-variable wake-up). Cause: inconclusive." },
            { c: "var(--info)", t: "Inconclusive", d: "Events were lost or the trace has gaps, so ThreadLens refuses to confirm." },
          ].map((x, i) => (
            <Reveal key={x.t} delay={i * 0.06}>
              <TiltCard className="h-full">
                <div className="card h-full p-5">
                  <span className="block h-1 w-10 rounded-full" style={{ background: x.c }} />
                  <div className="mt-4 font-semibold">{x.t}</div>
                  <p className="mt-2 text-sm leading-relaxed text-fg-2">{x.d}</p>
                </div>
              </TiltCard>
            </Reveal>
          ))}
        </div>
      </section>

      {/* ------------------------------------------------------------ scope */}
      <section id="scope" className="mx-auto max-w-7xl px-6 pb-28">
        <Reveal>
          <div className="eyebrow mb-3">Where it fits</div>
          <h2 className="max-w-3xl text-3xl font-semibold tracking-tight sm:text-4xl">Built on established ideas, focused on explanation.</h2>
          <p className="mt-4 max-w-3xl text-fg-2">
            Race detectors, deadlock predictors, kernel lock profilers and record-and-replay tools solve different problems. ThreadLens does not replace them; it integrates live pthread event collection, conservative deadlock classification, evidence and replay into one workflow.
          </p>
        </Reveal>
        <Reveal delay={0.1}>
          <div className="card mt-10 overflow-x-auto">
            <table className="w-full min-w-[760px] text-sm">
              <thead>
                <tr className="eyebrow border-b hairline text-left">
                  {["Tool", "Main purpose", "Deadlocks", "Live view", "Explains with events", "Typical cost"].map((h) => (
                    <th key={h} className="px-4 py-3 font-normal">{h}</th>
                  ))}
                </tr>
              </thead>
              <tbody className="text-fg-2">
                {[
                  ["Helgrind", "data races, lock order, API misuse", "potential (lock-order)", "—", "stack traces", "~100× slowdown (docs)"],
                  ["DRD", "data races, contention, API misuse", "—", "—", "stack traces", "20–50× (docs)"],
                  ["ThreadSanitizer", "data races (+ deadlock detector)", "potential (lock-order)", "—", "stack traces", "recompile"],
                  ["perf lock", "kernel lock contention", "—", "—", "statistics", "kernel tracing"],
                  ["rr", "deterministic record & replay", "re-execution", "—", "debugger", "single core"],
                  ["ThreadLens", "pthread synchronization debugging", "confirmed + potential", "yes", "ordered evidence + replay", "measured in docs/testing.md"],
                ].map((r) => (
                  <tr key={r[0]} className={cx("border-b hairline last:border-0", r[0] === "ThreadLens" && "bg-cyan/5 text-fg")}>
                    {r.map((c, i) => (
                      <td key={i} className={cx("px-4 py-3", i === 0 && "font-medium text-fg")}>{c}</td>
                    ))}
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
          <p className="mt-3 text-xs text-fg-3">Capabilities summarised from each tool's documentation; see docs/research-review.md for sources and nuances.</p>
        </Reveal>
      </section>

      {/* -------------------------------------------------------------- cta */}
      <section className="relative overflow-hidden border-t hairline">
        <div className="grid-bg absolute inset-0 opacity-60" />
        <div className="relative mx-auto flex max-w-7xl flex-col items-center px-6 py-24 text-center">
          <Reveal>
            <Logo size={44} />
          </Reveal>
          <Reveal delay={0.1}>
            <h2 className="mt-6 max-w-2xl text-3xl font-semibold tracking-tight sm:text-4xl">Launch a real program and watch the graph build itself.</h2>
          </Reveal>
          <Reveal delay={0.2}>
            <div className="mt-8 flex flex-wrap justify-center gap-3">
              <Link to="/app/lab" className="btn btn-primary px-5 py-2.5 text-base">Open the Test Lab <ArrowRight size={17} /></Link>
              <Link to="/app" className="btn px-5 py-2.5 text-base">Go to the dashboard</Link>
            </div>
          </Reveal>
        </div>
        <footer className="relative border-t hairline px-6 py-6 text-center text-xs text-fg-3">
          ThreadLens — an Operating Systems project. Event replay reconstructs analysis state; it does not re-execute programs.
        </footer>
      </section>
    </div>
  );
}

function HealthChip({ health }: { health: Health | null | "offline" }) {
  if (health === null) return <span className="chip">checking backend…</span>;
  if (health === "offline") return <span className="chip text-warn">backend offline — start scripts/run_backend.sh</span>;
  return (
    <span className="chip text-ok">
      <span className="h-1.5 w-1.5 rounded-full bg-ok" /> backend online · {health.scenarios_built} real test programs built
    </span>
  );
}

function StaticHero() {
  return (
    <div className="absolute inset-0 overflow-hidden">
      <div className="grid-bg absolute inset-0 opacity-70" />
      <div className="absolute left-1/2 top-[38%] h-[520px] w-[520px] -translate-x-1/2 -translate-y-1/2 rounded-full opacity-30 blur-3xl" style={{ background: "radial-gradient(circle, var(--cyan), transparent 60%)" }} />
      <div className="absolute left-[60%] top-[30%] h-[380px] w-[380px] rounded-full opacity-20 blur-3xl" style={{ background: "radial-gradient(circle, var(--violet), transparent 60%)" }} />
      <div className="absolute inset-x-0 top-[22%] text-center text-7xl font-bold tracking-tight opacity-90 sm:text-8xl">
        <span className="text-gradient">ThreadLens</span>
      </div>
    </div>
  );
}

const PIPE = [
  { icon: Binary, title: "Intercept", body: "libthreadlens.so is preloaded into the target. Its wrappers record pthread_mutex_lock, cond_wait, join… and forward to glibc via dlsym(RTLD_NEXT)." },
  { icon: Cpu, title: "Sequence", body: "Each record claims a slot in a lock-free ring; the slot index is a total order. Releases are recorded before the real unlock, acquisitions after the real lock." },
  { icon: Network, title: "Analyse", body: "The backend rebuilds ownership, the current wait-for graph and the historical lock-order graph, and checks for cycles incrementally." },
  { icon: Radar, title: "Explain", body: "Findings keep their evidence — ordered events, source lines from addr2line, the cycle — and can be replayed event by event." },
];

function Pipeline() {
  const ref = useRef<HTMLDivElement>(null);
  const { scrollYProgress } = useScroll({ target: ref, offset: ["start end", "end start"] });
  const y0 = useTransform(scrollYProgress, [0, 1], [40, -40]);
  const y1 = useTransform(scrollYProgress, [0, 1], [70, -70]);
  const ys = [y0, y1, y0, y1];
  return (
    <div ref={ref} className="relative mt-14 grid gap-5 md:grid-cols-2 lg:grid-cols-4">
      <div aria-hidden className="absolute left-0 right-0 top-1/2 hidden h-px -translate-y-1/2 lg:block" style={{ background: "linear-gradient(90deg, transparent, var(--cyan), var(--violet), transparent)", opacity: 0.35 }} />
      {PIPE.map((p, i) => (
        <motion.div key={p.title} style={{ y: ys[i] }}>
          <Reveal delay={i * 0.08}>
            <TiltCard>
              <div className="card relative p-6">
                <div className="flex items-center justify-between">
                  <div className="grid h-10 w-10 place-items-center rounded-xl border hairline bg-surface-2 text-cyan" style={{ boxShadow: "var(--glow-cyan)" }}>
                    <p.icon size={19} />
                  </div>
                  <span className="mono text-xs text-fg-3">0{i + 1}</span>
                </div>
                <div className="mt-5 text-lg font-semibold">{p.title}</div>
                <p className="mt-2 text-sm leading-relaxed text-fg-2">{p.body}</p>
              </div>
            </TiltCard>
          </Reveal>
        </motion.div>
      ))}
      <Reveal delay={0.3} className="md:col-span-2 lg:col-span-4">
        <div className="mt-4 flex flex-wrap items-center justify-center gap-x-6 gap-y-2 text-xs text-fg-3">
          <span className="inline-flex items-center gap-1.5"><Eye size={13} /> observed, not simulated</span>
          <span className="inline-flex items-center gap-1.5"><ShieldCheck size={13} /> lost events are counted and downgrade verdicts</span>
          <span className="inline-flex items-center gap-1.5"><GitCompare size={13} /> original vs corrected comparison</span>
          <span className="inline-flex items-center gap-1.5"><SparkIcon size={13} /> 2D and 3D views from the same analysis</span>
        </div>
      </Reveal>
    </div>
  );
}

function ComparePanel({ kind }: { kind: "original" | "fixed" }) {
  const orig = kind === "original";
  const color = orig ? "var(--crit)" : "var(--ok)";
  const A = { x: 80, y: 120 };
  const B = { x: 300, y: 120 };
  const L1 = { x: 190, y: 40 };
  const L2 = { x: 190, y: 200 };
  const edges = orig
    ? [
        [L1, A, false],
        [A, L2, true],
        [L2, B, false],
        [B, L1, true],
      ]
    : [
        [L1, A, false],
        [B, L1, true],
        [A, L2, false],
      ];
  return (
    <div className="card overflow-hidden" style={{ boxShadow: orig ? "var(--glow-crit)" : undefined }}>
      <div className="flex items-center justify-between border-b hairline px-5 py-3">
        <div>
          <div className="eyebrow">{orig ? "bank_transfer.c" : "bank_transfer_fixed.c"}</div>
          <div className="font-semibold">{orig ? "Original: source account first" : "Corrected: lower id first"}</div>
        </div>
        <span className="chip font-medium" style={{ color, borderColor: color }}>
          {orig ? "deadlock — killed after 1 s" : "completes"}
        </span>
      </div>
      <div className="grid gap-4 p-5 sm:grid-cols-[1fr_1.1fr]">
        <svg viewBox="0 0 380 240" className="w-full" role="img" aria-label={orig ? "cycle between two threads" : "no cycle"}>
          <defs>
            <marker id={`cmp-${kind}`} viewBox="0 0 10 10" refX="8" refY="5" markerWidth="7" markerHeight="7" orient="auto-start-reverse">
              <path d="M0,0 L10,5 L0,10 z" fill="context-stroke" />
            </marker>
          </defs>
          {edges.map(([f, t, wait], i) => {
            const from = f as { x: number; y: number };
            const to = t as { x: number; y: number };
            const dx = to.x - from.x;
            const dy = to.y - from.y;
            const len = Math.hypot(dx, dy);
            const s = { x: from.x + (dx / len) * 30, y: from.y + (dy / len) * 26 };
            const e = { x: to.x - (dx / len) * 34, y: to.y - (dy / len) * 28 };
            const c = orig ? "var(--crit)" : wait ? "var(--warn)" : "var(--violet)";
            return (
              <motion.line
                key={i}
                x1={s.x}
                y1={s.y}
                x2={e.x}
                y2={e.y}
                stroke={c}
                strokeWidth={2.4}
                strokeDasharray={wait ? "6 4" : undefined}
                markerEnd={`url(#cmp-${kind})`}
                initial={{ opacity: 0 }}
                whileInView={{ opacity: 1 }}
                viewport={{ once: true }}
                transition={{ delay: 0.3 + i * 0.25 }}
                style={wait ? { animation: "var(--animate-dash)" } : undefined}
              />
            );
          })}
          {[
            [A, "A"],
            [B, "B"],
          ].map(([p, l]) => (
            <g key={l as string}>
              <circle cx={(p as { x: number }).x} cy={(p as { y: number }).y} r={24} fill="var(--surface-2)" stroke={orig ? "var(--crit)" : "var(--cyan)"} strokeWidth={2.5} />
              <text x={(p as { x: number }).x} y={(p as { y: number }).y + 5} textAnchor="middle" fontSize={14} fontWeight={600} fill="var(--fg)">{l as string}</text>
            </g>
          ))}
          {[
            [L1, "L1"],
            [L2, "L2"],
          ].map(([p, l]) => (
            <g key={l as string}>
              <rect x={(p as { x: number }).x - 30} y={(p as { y: number }).y - 18} width={60} height={36} rx={9} fill="var(--surface-2)" stroke={orig ? "var(--crit)" : "var(--violet)"} strokeWidth={2.5} />
              <text x={(p as { x: number }).x} y={(p as { y: number }).y + 5} textAnchor="middle" fontSize={13} fill="var(--fg)">{l as string}</text>
            </g>
          ))}
        </svg>
        <pre className="mono overflow-x-auto rounded-lg border hairline bg-bg-elev p-3 text-[11.5px] leading-relaxed text-fg-2">
          {orig ? (
            <>
              <span className="text-fg-3">{"// teller-1: A→B   teller-2: B→A"}</span>
              {"\n"}
              <span className="text-cyan">pthread_mutex_lock</span>(&from-&gt;lock);{"\n"}
              <span className="text-cyan">pthread_mutex_lock</span>(&to-&gt;lock);
              <span className="text-crit">{"  // ✗ order depends on direction"}</span>
            </>
          ) : (
            <>
              <span className="text-fg-3">{"// FIX: one global order"}</span>
              {"\n"}first = from-&gt;id &lt; to-&gt;id ? from : to;{"\n"}
              <span className="text-cyan">pthread_mutex_lock</span>(&first-&gt;lock);{"\n"}
              <span className="text-cyan">pthread_mutex_lock</span>(&second-&gt;lock);
              <span className="text-ok">{"  // ✓"}</span>
            </>
          )}
        </pre>
      </div>
    </div>
  );
}
