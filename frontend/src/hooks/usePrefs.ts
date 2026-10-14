import { useEffect, useState } from "react";

import { createStore, useStore } from "@/services/store";

export interface Prefs {
  theme: "dark" | "light";
  effects: "system" | "full" | "reduced";
  graphMode: "2d" | "3d";
  timeUnit: "ms" | "us" | "ns";
  liveEventLimit: number;
  ringCapacity: number;
  blockUs: number;
  killOnDeadlock: boolean;
  showFilteredFindings: boolean;
  navCollapsed: boolean;
}

const KEY = "threadlens.prefs";
const defaults: Prefs = {
  theme: "dark",
  effects: "system",
  graphMode: "2d",
  timeUnit: "ms",
  liveEventLimit: 300,
  ringCapacity: 65536,
  blockUs: 100000,
  killOnDeadlock: true,
  showFilteredFindings: false,
  navCollapsed: false,
};

function load(): Prefs {
  try {
    const raw = localStorage.getItem(KEY);
    return raw ? { ...defaults, ...JSON.parse(raw) } : defaults;
  } catch {
    return defaults;
  }
}

export const prefsStore = createStore<Prefs>(load());

prefsStore.subscribe(() => {
  try {
    localStorage.setItem(KEY, JSON.stringify(prefsStore.get()));
  } catch {
    /* private mode: preferences last for this tab only */
  }
  applyPrefs();
});

export function applyPrefs() {
  const p = prefsStore.get();
  document.documentElement.dataset.theme = p.theme;
  document.documentElement.dataset.effects = p.effects;
}

export function usePrefs<S>(selector: (p: Prefs) => S): S {
  return useStore(prefsStore, selector);
}

export function setPrefs(patch: Partial<Prefs>) {
  prefsStore.set(patch);
}

function useMedia(query: string): boolean {
  const [match, setMatch] = useState(() => window.matchMedia?.(query).matches ?? false);
  useEffect(() => {
    const mq = window.matchMedia?.(query);
    if (!mq) return;
    const on = () => setMatch(mq.matches);
    mq.addEventListener("change", on);
    return () => mq.removeEventListener("change", on);
  }, [query]);
  return match;
}

/** True when motion and decorative effects should be minimised. */
export function useReducedEffects(): boolean {
  const effects = usePrefs((p) => p.effects);
  const osReduced = useMedia("(prefers-reduced-motion: reduce)");
  return effects === "reduced" || (effects === "system" && osReduced);
}

/** WebGL availability (3D views fall back to 2D when it is missing). */
export function hasWebGL(): boolean {
  try {
    const c = document.createElement("canvas");
    return !!(c.getContext("webgl2") || c.getContext("webgl"));
  } catch {
    return false;
  }
}
