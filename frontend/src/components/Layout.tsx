import { AnimatePresence, motion } from "motion/react";
import {
  Activity,
  AlertTriangle,
  ChevronDown,
  ChevronsLeft,
  ChevronsRight,
  FlaskConical,
  GitFork,
  History,
  LayoutDashboard,
  ListTree,
  Network,
  Settings as SettingsIcon,
  Square,
  X,
} from "lucide-react";
import { type ReactNode, useEffect, useRef, useState } from "react";
import { Link, NavLink, useLocation, useNavigate } from "react-router-dom";

import { Logo } from "@/components/Logo";
import { StatusDot, VerdictPill, cx } from "@/components/ui";
import { usePrefs, setPrefs } from "@/hooks/usePrefs";
import { api } from "@/services/api";
import { openSession, sessionStore } from "@/services/session";
import { useStore } from "@/services/store";
import { toast, toastStore } from "@/services/toast";
import type { SessionInfo } from "@/types/api";
import { fmtMs, timeAgo } from "@/utils/format";

const NAV = [
  { to: "/app", label: "Overview", icon: LayoutDashboard, end: true },
  { to: "/app/threads", label: "Live Threads", icon: Activity },
  { to: "/app/graph", label: "Lock Graph", icon: Network },
  { to: "/app/events", label: "Event Explorer", icon: ListTree },
  { to: "/app/diagnostics", label: "Diagnostics", icon: AlertTriangle },
  { to: "/app/replay", label: "Replay & Compare", icon: History },
  { to: "/app/lab", label: "Test Lab", icon: FlaskConical },
  { to: "/app/settings", label: "Settings", icon: SettingsIcon },
];

export function AppLayout({ children }: { children: ReactNode }) {
  const collapsed = usePrefs((p) => p.navCollapsed);
  const location = useLocation();
  return (
    <div className="flex h-full min-h-0 bg-bg text-fg">
      <Sidebar collapsed={collapsed} />
      <div className="flex min-w-0 flex-1 flex-col">
        <Topbar />
        <main className="relative min-h-0 flex-1 overflow-y-auto">
          <div className="grid-bg pointer-events-none absolute inset-x-0 top-0 h-64 opacity-60 [mask-image:linear-gradient(to_bottom,black,transparent)]" />
          {/* enter-only page transition, so navigation never waits on an exit animation */}
          <AnimatePresence initial={false}>
            <motion.div
              key={location.pathname}
              initial={{ opacity: 0, y: 8 }}
              animate={{ opacity: 1, y: 0 }}
              transition={{ duration: 0.22, ease: [0.16, 1, 0.3, 1] }}
              className="relative mx-auto w-full max-w-[1600px] px-4 py-6 sm:px-6 lg:px-8"
            >
              {children}
            </motion.div>
          </AnimatePresence>
        </main>
      </div>
      <Toaster />
    </div>
  );
}

function Sidebar({ collapsed }: { collapsed: boolean }) {
  const alerts = useStore(sessionStore, (s) =>
    (s.snapshot?.findings ?? []).filter((f) => f.severity !== "info" && f.status !== "filtered").length,
  );
  const critical = useStore(sessionStore, (s) => (s.snapshot?.findings ?? []).some((f) => f.severity === "critical" && f.status !== "resolved"));
  return (
    <motion.nav
      aria-label="Main"
      className="relative z-20 hidden shrink-0 flex-col border-r hairline bg-bg-elev md:flex"
      animate={{ width: collapsed ? 68 : 232 }}
      transition={{ type: "spring", stiffness: 400, damping: 40 }}
    >
      <Link to="/" className="flex h-14 items-center gap-2.5 border-b hairline px-4" title="ThreadLens home">
        <Logo size={26} />
        <AnimatePresence initial={false}>
          {!collapsed && (
            <motion.span initial={{ opacity: 0, x: -6 }} animate={{ opacity: 1, x: 0 }} exit={{ opacity: 0, x: -6 }} className="text-[15px] font-semibold tracking-tight">
              Thread<span className="text-cyan">Lens</span>
            </motion.span>
          )}
        </AnimatePresence>
      </Link>
      <div className="flex-1 space-y-0.5 overflow-y-auto p-2.5">
        {NAV.map(({ to, label, icon: Icon, end }) => (
          <NavLink
            key={to}
            to={to}
            end={end}
            title={collapsed ? label : undefined}
            className={({ isActive }) =>
              cx(
                "group relative flex items-center gap-3 rounded-[10px] px-3 py-2 text-sm transition-colors",
                isActive ? "text-fg" : "text-fg-3 hover:bg-surface hover:text-fg-2",
              )
            }
          >
            {({ isActive }) => (
              <>
                {isActive && (
                  <motion.span
                    layoutId="nav-active"
                    className="absolute inset-0 rounded-[10px] border hairline bg-surface-2"
                    transition={{ type: "spring", stiffness: 500, damping: 40 }}
                  />
                )}
                {isActive && <motion.span layoutId="nav-bar" className="absolute left-0 top-2 bottom-2 w-[2px] rounded-full bg-cyan" />}
                <Icon size={17} className={cx("relative z-10 shrink-0", isActive && "text-cyan")} />
                {!collapsed && <span className="relative z-10 truncate">{label}</span>}
                {label === "Diagnostics" && alerts > 0 && (
                  <span
                    className={cx("relative z-10 ml-auto rounded-full px-1.5 text-[10px] font-semibold tabular-nums", collapsed && "absolute right-1 top-1")}
                    style={{ background: critical ? "var(--crit)" : "var(--warn)", color: "#0b0f19" }}
                  >
                    {alerts}
                  </span>
                )}
              </>
            )}
          </NavLink>
        ))}
      </div>
      <button
        className="m-2.5 flex items-center gap-2 rounded-[10px] px-3 py-2 text-xs text-fg-3 hover:bg-surface hover:text-fg-2"
        onClick={() => setPrefs({ navCollapsed: !collapsed })}
        aria-label={collapsed ? "Expand navigation" : "Collapse navigation"}
      >
        {collapsed ? <ChevronsRight size={16} /> : <ChevronsLeft size={16} />}
        {!collapsed && "Collapse"}
      </button>
    </motion.nav>
  );
}

function Topbar() {
  const info = useStore(sessionStore, (s) => s.info);
  const verdict = useStore(sessionStore, (s) => s.snapshot?.verdict.classification ?? s.info?.verdict ?? null);
  const connection = useStore(sessionStore, (s) => s.connection);
  const running = info?.status === "running" || info?.status === "starting";
  const [stopping, setStopping] = useState(false);
  return (
    <header className="sticky top-0 z-10 flex h-14 shrink-0 items-center gap-3 border-b hairline bg-bg/80 px-4 backdrop-blur-xl sm:px-6">
      <MobileNav />
      <SessionSwitcher current={info} />
      <div className="hidden items-center gap-2 lg:flex">
        {info && <VerdictPill verdict={verdict} />}
        {connection === "live" && (
          <span className="chip text-cyan" title="Receiving live updates over WebSocket">
            <StatusDot status="running" /> live
          </span>
        )}
        {connection === "error" && <span className="chip text-crit">connection error</span>}
      </div>
      <div className="ml-auto flex items-center gap-2">
        {running && info && (
          <button
            className="btn btn-danger"
            disabled={stopping}
            onClick={async () => {
              setStopping(true);
              try {
                await api.stopSession(info.id);
                toast.push({ level: "info", title: "Session stopped", body: "The monitored process group was terminated." });
              } catch (e) {
                toast.push({ level: "critical", title: "Could not stop session", body: (e as Error).message });
              } finally {
                setStopping(false);
              }
            }}
          >
            <Square size={13} /> Stop
          </button>
        )}
        <Link to="/app/lab" className="btn btn-primary">
          <FlaskConical size={15} /> <span className="hidden sm:inline">New session</span>
        </Link>
      </div>
    </header>
  );
}

function SessionSwitcher({ current }: { current: SessionInfo | null }) {
  const [open, setOpen] = useState(false);
  const [list, setList] = useState<SessionInfo[] | null>(null);
  const ref = useRef<HTMLDivElement>(null);
  const navigate = useNavigate();
  useEffect(() => {
    if (!open) return;
    api.sessions(30).then(setList).catch(() => setList([]));
    const onDoc = (e: MouseEvent) => ref.current && !ref.current.contains(e.target as Node) && setOpen(false);
    document.addEventListener("mousedown", onDoc);
    return () => document.removeEventListener("mousedown", onDoc);
  }, [open]);
  return (
    <div ref={ref} className="relative min-w-0">
      <button
        className="flex min-w-0 max-w-[52vw] items-center gap-2.5 rounded-[10px] border hairline bg-surface px-3 py-1.5 text-left hover:border-line-strong sm:max-w-md"
        onClick={() => setOpen((o) => !o)}
        aria-haspopup="listbox"
        aria-expanded={open}
      >
        {current ? <StatusDot status={current.status} /> : <span className="h-2 w-2 rounded-full bg-fg-3" />}
        <span className="min-w-0">
          <span className="block truncate text-sm font-medium">{current?.label ?? current?.id ?? "No session selected"}</span>
          {current && <span className="mono block truncate text-[11px] text-fg-3">{current.id}</span>}
        </span>
        <ChevronDown size={15} className="shrink-0 text-fg-3" />
      </button>
      <AnimatePresence>
        {open && (
          <motion.div
            role="listbox"
            className="card absolute left-0 top-full z-30 mt-2 max-h-[70vh] w-[min(440px,92vw)] overflow-y-auto bg-surface p-1.5"
            initial={{ opacity: 0, y: -6, scale: 0.98 }}
            animate={{ opacity: 1, y: 0, scale: 1 }}
            exit={{ opacity: 0, y: -6, scale: 0.98 }}
            transition={{ duration: 0.16 }}
          >
            {list === null && <div className="p-3 text-sm text-fg-3">Loading sessions…</div>}
            {list?.length === 0 && (
              <div className="p-3 text-sm text-fg-3">
                No sessions yet. <Link to="/app/lab" className="text-cyan" onClick={() => setOpen(false)}>Start one in the Test Lab</Link>.
              </div>
            )}
            {list?.map((s) => (
              <button
                key={s.id}
                role="option"
                aria-selected={s.id === current?.id}
                onClick={() => {
                  openSession(s.id);
                  setOpen(false);
                  navigate("/app");
                }}
                className={cx("flex w-full items-center gap-3 rounded-lg px-3 py-2 text-left hover:bg-surface-2", s.id === current?.id && "bg-surface-2")}
              >
                <StatusDot status={s.status} />
                <span className="min-w-0 flex-1">
                  <span className="block truncate text-sm">{s.label ?? s.id}</span>
                  <span className="mono block truncate text-[11px] text-fg-3">
                    {s.id} · {timeAgo(s.created_at)} · {s.event_count.toLocaleString()} ev{s.wall_ms ? ` · ${fmtMs(s.wall_ms)}` : ""}
                  </span>
                </span>
                <VerdictPill verdict={s.verdict} />
              </button>
            ))}
          </motion.div>
        )}
      </AnimatePresence>
    </div>
  );
}

function MobileNav() {
  const [open, setOpen] = useState(false);
  return (
    <div className="md:hidden">
      <button className="btn btn-ghost p-1.5" onClick={() => setOpen(true)} aria-label="Open navigation">
        <GitFork size={18} />
      </button>
      <AnimatePresence>
        {open && (
          <motion.div className="fixed inset-0 z-50 bg-bg/95 p-4 backdrop-blur" initial={{ opacity: 0 }} animate={{ opacity: 1 }} exit={{ opacity: 0 }}>
            <div className="mb-4 flex items-center justify-between">
              <Logo size={26} />
              <button className="btn btn-ghost p-1.5" onClick={() => setOpen(false)} aria-label="Close navigation">
                <X size={18} />
              </button>
            </div>
            {NAV.map(({ to, label, icon: Icon, end }) => (
              <NavLink key={to} to={to} end={end} onClick={() => setOpen(false)} className={({ isActive }) => cx("flex items-center gap-3 rounded-lg px-3 py-3 text-base", isActive ? "bg-surface-2 text-fg" : "text-fg-2")}>
                <Icon size={18} /> {label}
              </NavLink>
            ))}
          </motion.div>
        )}
      </AnimatePresence>
    </div>
  );
}

function Toaster() {
  const toasts = useStore(toastStore, (s) => s.toasts);
  const color = { critical: "var(--crit)", warning: "var(--warn)", info: "var(--info)", success: "var(--ok)" };
  return (
    <div className="pointer-events-none fixed bottom-4 right-4 z-[60] flex w-[min(380px,calc(100vw-2rem))] flex-col gap-2" aria-live="polite">
      <AnimatePresence initial={false}>
        {toasts.map((t) => (
          <motion.div
            key={t.id}
            layout
            initial={{ opacity: 0, x: 40, scale: 0.96 }}
            animate={{ opacity: 1, x: 0, scale: 1 }}
            exit={{ opacity: 0, x: 40, scale: 0.96 }}
            transition={{ type: "spring", stiffness: 420, damping: 34 }}
            className="card pointer-events-auto relative overflow-hidden bg-surface p-3.5 pl-4"
            style={{ boxShadow: t.level === "critical" ? "var(--glow-crit)" : undefined }}
          >
            <span className="absolute inset-y-0 left-0 w-[3px]" style={{ background: color[t.level] }} />
            <div className="flex items-start gap-3">
              <div className="min-w-0 flex-1">
                <div className="text-sm font-semibold" style={{ color: color[t.level] }}>{t.title}</div>
                {t.body && <div className="mt-0.5 text-sm text-fg-2">{t.body}</div>}
                {t.action && (
                  <Link to={t.action.href} className="mt-2 inline-block text-xs font-medium text-cyan hover:underline" onClick={() => toast.dismiss(t.id)}>
                    {t.action.label} →
                  </Link>
                )}
              </div>
              <button className="text-fg-3 hover:text-fg" onClick={() => toast.dismiss(t.id)} aria-label="Dismiss">
                <X size={14} />
              </button>
            </div>
          </motion.div>
        ))}
      </AnimatePresence>
    </div>
  );
}
