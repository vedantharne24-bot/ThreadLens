import { AnimatePresence, motion, useMotionValue, useSpring, useTransform } from "motion/react";
import { X } from "lucide-react";
import { type ReactNode, useEffect, useRef, useState } from "react";

import { useReducedEffects } from "@/hooks/usePrefs";
import type { Classification, Severity, ThreadState } from "@/types/api";
import { SEVERITY_META, STATE_META, VERDICT_META } from "@/utils/format";

export function cx(...parts: (string | false | null | undefined)[]): string {
  return parts.filter(Boolean).join(" ");
}

export function Card({ className, children, glow }: { className?: string; children: ReactNode; glow?: "cyan" | "crit" }) {
  return (
    <div
      className={cx("card", className)}
      style={glow ? { boxShadow: glow === "crit" ? "var(--glow-crit)" : "var(--glow-cyan)" } : undefined}
    >
      {children}
    </div>
  );
}

export function CardHeader({ title, eyebrow, actions, icon }: { title: ReactNode; eyebrow?: string; actions?: ReactNode; icon?: ReactNode }) {
  return (
    <div className="flex items-center justify-between gap-3 border-b hairline px-4 py-3">
      <div className="flex min-w-0 items-center gap-2.5">
        {icon && <span className="text-fg-3">{icon}</span>}
        <div className="min-w-0">
          {eyebrow && <div className="eyebrow mb-0.5">{eyebrow}</div>}
          <h3 className="truncate text-sm font-semibold tracking-tight text-fg">{title}</h3>
        </div>
      </div>
      {actions && <div className="flex shrink-0 items-center gap-1.5">{actions}</div>}
    </div>
  );
}

/** Number that animates toward the latest real value (never a fake value). */
export function Counter({ value, format = (n) => Math.round(n).toLocaleString() }: { value: number | null | undefined; format?: (n: number) => string }) {
  const reduced = useReducedEffects();
  const mv = useMotionValue(value ?? 0);
  const spring = useSpring(mv, { stiffness: 120, damping: 22, mass: 0.6 });
  const text = useTransform(reduced ? mv : spring, (v) => format(v));
  useEffect(() => {
    if (value !== null && value !== undefined) mv.set(value);
  }, [value, mv]);
  if (value === null || value === undefined) return <span className="text-fg-3">—</span>;
  return <motion.span className="tabular-nums">{text}</motion.span>;
}

export function StatTile({
  label,
  value,
  format,
  sub,
  icon,
  tone = "default",
  unavailable,
  children,
}: {
  label: string;
  value: number | null | undefined;
  format?: (n: number) => string;
  sub?: ReactNode;
  icon?: ReactNode;
  tone?: "default" | "crit" | "warn" | "ok" | "cyan";
  unavailable?: string;
  children?: ReactNode;
}) {
  const toneColor = { default: "var(--fg)", crit: "var(--crit)", warn: "var(--warn)", ok: "var(--ok)", cyan: "var(--cyan)" }[tone];
  return (
    <div className="card relative overflow-hidden p-4">
      <div className="flex items-center justify-between">
        <span className="eyebrow">{label}</span>
        {icon && <span className="text-fg-3">{icon}</span>}
      </div>
      <div className="mt-2 text-2xl font-semibold tracking-tight" style={{ color: toneColor }}>
        {unavailable ? <span className="text-base font-normal text-fg-3" title={unavailable}>unavailable</span> : <Counter value={value} format={format} />}
      </div>
      {sub && <div className="mt-1 text-xs text-fg-3">{sub}</div>}
      {children}
      {tone !== "default" && (
        <div
          aria-hidden
          className="pointer-events-none absolute -right-10 -top-10 h-28 w-28 rounded-full opacity-[0.12] blur-2xl"
          style={{ background: toneColor }}
        />
      )}
    </div>
  );
}

export function StateBadge({ state, inCycle }: { state: ThreadState; inCycle?: boolean }) {
  const m = STATE_META[state] ?? STATE_META.running;
  const color = inCycle ? "var(--crit)" : m.color;
  return (
    <span className="chip" style={{ borderColor: `color-mix(in oklab, ${color} 35%, transparent)`, color }}>
      <span className="relative inline-flex h-1.5 w-1.5">
        {(state === "blocked_lock" || inCycle) && (
          <span className="absolute inset-0 rounded-full" style={{ color, animation: "var(--animate-pulse-ring)" }} />
        )}
        <span className="h-1.5 w-1.5 rounded-full" style={{ background: color }} />
      </span>
      {inCycle ? "Deadlocked" : m.label}
    </span>
  );
}

export function SeverityBadge({ severity }: { severity: Severity }) {
  const m = SEVERITY_META[severity];
  return (
    <span className="chip font-medium" style={{ color: m.color, borderColor: `color-mix(in oklab, ${m.color} 40%, transparent)` }}>
      <span className="h-1.5 w-1.5 rounded-full" style={{ background: m.color }} />
      {m.label}
    </span>
  );
}

export function VerdictPill({ verdict, size = "sm" }: { verdict: Classification | null | undefined; size?: "sm" | "lg" }) {
  if (!verdict) return <span className="chip text-fg-3">no verdict</span>;
  const m = VERDICT_META[verdict];
  return (
    <span
      className={cx("chip font-medium", size === "lg" && "px-3 py-1 text-sm")}
      style={{ color: m.color, borderColor: `color-mix(in oklab, ${m.color} 40%, transparent)`, background: `color-mix(in oklab, ${m.color} 9%, var(--surface-2))` }}
    >
      <span className="h-1.5 w-1.5 rounded-full" style={{ background: m.color }} />
      {m.short}
    </span>
  );
}

export function StatusDot({ status }: { status: string }) {
  const color =
    status === "running" || status === "starting"
      ? "var(--cyan)"
      : status === "completed"
        ? "var(--ok)"
        : status === "killed" || status === "failed" || status === "error"
          ? "var(--crit)"
          : "var(--fg-3)";
  return (
    <span className="relative inline-flex h-2 w-2" aria-label={status}>
      {status === "running" && <span className="absolute inset-0 rounded-full" style={{ color, animation: "var(--animate-pulse-ring)" }} />}
      <span className="h-2 w-2 rounded-full" style={{ background: color }} />
    </span>
  );
}

export function ThreadChip({ index, label, onClick, tone }: { index: number; label?: string; onClick?: () => void; tone?: string }) {
  return (
    <button
      type="button"
      onClick={onClick}
      disabled={!onClick}
      className="chip mono hover:border-cyan/50 disabled:cursor-default"
      style={tone ? { color: tone, borderColor: `color-mix(in oklab, ${tone} 40%, transparent)` } : undefined}
    >
      <span className="text-fg-3">T{index}</span>
      {label && <span>{label}</span>}
    </button>
  );
}

export function LockChip({ id, label, onClick, tone }: { id: number; label?: string; onClick?: () => void; tone?: string }) {
  return (
    <button
      type="button"
      onClick={onClick}
      disabled={!onClick}
      className="chip mono hover:border-violet/50 disabled:cursor-default"
      style={tone ? { color: tone, borderColor: `color-mix(in oklab, ${tone} 40%, transparent)` } : { color: "var(--violet)" }}
    >
      <span className="opacity-60">L{id}</span>
      {label && <span>{label}</span>}
    </button>
  );
}

export function Segmented<T extends string>({ value, options, onChange, size = "sm" }: { value: T; options: { value: T; label: ReactNode; title?: string }[]; onChange: (v: T) => void; size?: "sm" | "md" }) {
  return (
    <div className="inline-flex rounded-[10px] border hairline bg-bg-elev p-0.5" role="radiogroup">
      {options.map((o) => {
        const active = o.value === value;
        return (
          <button
            key={o.value}
            role="radio"
            aria-checked={active}
            title={o.title}
            onClick={() => onChange(o.value)}
            className={cx(
              "relative rounded-[8px] font-medium transition-colors",
              size === "sm" ? "px-2.5 py-1 text-xs" : "px-3.5 py-1.5 text-sm",
              active ? "text-fg" : "text-fg-3 hover:text-fg-2",
            )}
          >
            {active && (
              <motion.span
                layoutId={`seg-${options.map((x) => x.value).join("-")}`}
                className="absolute inset-0 rounded-[8px] border hairline bg-surface-3"
                transition={{ type: "spring", stiffness: 500, damping: 40 }}
              />
            )}
            <span className="relative z-10 inline-flex items-center gap-1.5">{o.label}</span>
          </button>
        );
      })}
    </div>
  );
}

export function Toggle({ checked, onChange, label }: { checked: boolean; onChange: (v: boolean) => void; label: string }) {
  return (
    <button
      type="button"
      role="switch"
      aria-checked={checked}
      aria-label={label}
      onClick={() => onChange(!checked)}
      className={cx("relative h-5 w-9 rounded-full border transition-colors", checked ? "border-transparent bg-cyan" : "hairline bg-surface-3")}
    >
      <motion.span
        className="absolute top-0.5 h-3.5 w-3.5 rounded-full bg-white shadow"
        animate={{ left: checked ? 18 : 3 }}
        transition={{ type: "spring", stiffness: 600, damping: 35 }}
      />
    </button>
  );
}

export function EmptyState({ icon, title, body, action }: { icon?: ReactNode; title: string; body?: ReactNode; action?: ReactNode }) {
  return (
    <div className="flex flex-col items-center justify-center px-6 py-14 text-center">
      {icon && (
        <div className="mb-4 grid h-12 w-12 place-items-center rounded-2xl border hairline bg-surface-2 text-fg-2" style={{ boxShadow: "var(--glow-cyan)" }}>
          {icon}
        </div>
      )}
      <h3 className="text-base font-semibold text-fg">{title}</h3>
      {body && <div className="mt-1.5 max-w-md text-sm text-fg-2">{body}</div>}
      {action && <div className="mt-5">{action}</div>}
    </div>
  );
}

export function Skeleton({ className }: { className?: string }) {
  return <div className={cx("skeleton", className)} />;
}

export function Drawer({ open, onClose, title, children, width = 480 }: { open: boolean; onClose: () => void; title: ReactNode; children: ReactNode; width?: number }) {
  useEffect(() => {
    if (!open) return;
    const onKey = (e: KeyboardEvent) => e.key === "Escape" && onClose();
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [open, onClose]);
  return (
    <AnimatePresence>
      {open && (
        <>
          <motion.div className="fixed inset-0 z-40 bg-black/40 backdrop-blur-[2px]" initial={{ opacity: 0 }} animate={{ opacity: 1 }} exit={{ opacity: 0 }} onClick={onClose} />
          <motion.aside
            role="dialog"
            aria-modal="true"
            className="fixed right-0 top-0 z-50 flex h-full max-w-[100vw] flex-col border-l hairline bg-surface shadow-2xl"
            style={{ width }}
            initial={{ x: width }}
            animate={{ x: 0 }}
            exit={{ x: width }}
            transition={{ type: "spring", stiffness: 380, damping: 40 }}
          >
            <div className="flex items-center justify-between border-b hairline px-5 py-3.5">
              <div className="min-w-0 text-sm font-semibold">{title}</div>
              <button className="btn btn-ghost p-1.5" onClick={onClose} aria-label="Close">
                <X size={16} />
              </button>
            </div>
            <div className="min-h-0 flex-1 overflow-y-auto">{children}</div>
          </motion.aside>
        </>
      )}
    </AnimatePresence>
  );
}

export function Modal({ open, onClose, title, children, wide }: { open: boolean; onClose: () => void; title: ReactNode; children: ReactNode; wide?: boolean }) {
  useEffect(() => {
    if (!open) return;
    const onKey = (e: KeyboardEvent) => e.key === "Escape" && onClose();
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [open, onClose]);
  return (
    <AnimatePresence>
      {open && (
        <motion.div className="fixed inset-0 z-50 grid place-items-center bg-black/50 p-4 backdrop-blur-sm" initial={{ opacity: 0 }} animate={{ opacity: 1 }} exit={{ opacity: 0 }} onClick={onClose}>
          <motion.div
            role="dialog"
            aria-modal="true"
            className={cx("card flex max-h-[88vh] w-full flex-col overflow-hidden bg-surface", wide ? "max-w-6xl" : "max-w-2xl")}
            initial={{ y: 16, scale: 0.98, opacity: 0 }}
            animate={{ y: 0, scale: 1, opacity: 1 }}
            exit={{ y: 8, scale: 0.98, opacity: 0 }}
            transition={{ type: "spring", stiffness: 400, damping: 36 }}
            onClick={(e) => e.stopPropagation()}
          >
            <div className="flex items-center justify-between border-b hairline px-5 py-3.5">
              <div className="min-w-0 text-sm font-semibold">{title}</div>
              <button className="btn btn-ghost p-1.5" onClick={onClose} aria-label="Close">
                <X size={16} />
              </button>
            </div>
            <div className="min-h-0 flex-1 overflow-auto">{children}</div>
          </motion.div>
        </motion.div>
      )}
    </AnimatePresence>
  );
}

/** Subtle pointer-driven perspective tilt. Disabled with reduced effects. */
export function TiltCard({ children, className, max = 6 }: { children: ReactNode; className?: string; max?: number }) {
  const reduced = useReducedEffects();
  const ref = useRef<HTMLDivElement>(null);
  const rx = useSpring(0, { stiffness: 200, damping: 20 });
  const ry = useSpring(0, { stiffness: 200, damping: 20 });
  const [hover, setHover] = useState(false);
  if (reduced) return <div className={className}>{children}</div>;
  return (
    <motion.div
      ref={ref}
      className={cx("tilt", className)}
      style={{ rotateX: rx, rotateY: ry, transformPerspective: 900 }}
      onPointerMove={(e) => {
        const r = ref.current?.getBoundingClientRect();
        if (!r) return;
        const px = (e.clientX - r.left) / r.width - 0.5;
        const py = (e.clientY - r.top) / r.height - 0.5;
        ry.set(px * max);
        rx.set(-py * max);
      }}
      onPointerEnter={() => setHover(true)}
      onPointerLeave={() => {
        setHover(false);
        rx.set(0);
        ry.set(0);
      }}
      whileHover={{ y: -2 }}
      data-hover={hover}
    >
      {children}
    </motion.div>
  );
}

export function KV({ items }: { items: [ReactNode, ReactNode][] }) {
  return (
    <dl className="grid grid-cols-[max-content_1fr] gap-x-4 gap-y-1.5 text-sm">
      {items.map(([k, v], i) => (
        <div key={i} className="contents">
          <dt className="text-fg-3">{k}</dt>
          <dd className="min-w-0 break-words text-fg">{v}</dd>
        </div>
      ))}
    </dl>
  );
}

export function Tip({ text, children }: { text: string; children: ReactNode }) {
  return (
    <span className="group relative inline-flex">
      {children}
      <span
        role="tooltip"
        className="pointer-events-none absolute bottom-full left-1/2 z-50 mb-2 w-max max-w-64 -translate-x-1/2 rounded-lg border hairline bg-surface-3 px-2.5 py-1.5 text-xs leading-snug text-fg-2 opacity-0 shadow-xl transition-opacity duration-150 group-hover:opacity-100 group-focus-within:opacity-100"
      >
        {text}
      </span>
    </span>
  );
}

export function PageHeader({ eyebrow, title, description, actions }: { eyebrow?: string; title: string; description?: ReactNode; actions?: ReactNode }) {
  return (
    <div className="mb-6 flex flex-wrap items-end justify-between gap-4">
      <div className="min-w-0">
        {eyebrow && <div className="eyebrow mb-1.5">{eyebrow}</div>}
        <h1 className="text-2xl font-semibold tracking-tight text-fg">{title}</h1>
        {description && <p className="mt-1.5 max-w-3xl text-sm text-fg-2">{description}</p>}
      </div>
      {actions && <div className="flex flex-wrap items-center gap-2">{actions}</div>}
    </div>
  );
}
