import { createStore } from "@/services/store";

export interface Toast {
  id: number;
  level: "critical" | "warning" | "info" | "success";
  title: string;
  body?: string;
  action?: { label: string; href: string };
}

export const toastStore = createStore<{ toasts: Toast[] }>({ toasts: [] });
let next = 1;

export const toast = {
  push(t: Omit<Toast, "id">, ttl = 6500) {
    const id = next++;
    toastStore.set((s) => ({ toasts: [...s.toasts.slice(-3), { ...t, id }] }));
    window.setTimeout(() => toast.dismiss(id), ttl);
    return id;
  },
  dismiss(id: number) {
    toastStore.set((s) => ({ toasts: s.toasts.filter((x) => x.id !== id) }));
  },
};
