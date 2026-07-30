import { useEffect, useRef, useState } from "react";
import type { RoleHealth, ToastNotification } from "../api/types";

const VISIBLE_TOASTS = 4;
const RETAINED_TOASTS = 20;
const TOAST_LIFETIME_MS = 15_000;

type ActiveToast = ToastNotification & { expiresAt: number };

function activeToast(notification: ToastNotification): ActiveToast {
  return {
    ...notification,
    expiresAt: Date.now() + TOAST_LIFETIME_MS,
  };
}

export function ToastStack({
  repo,
  notifications,
  roles,
}: {
  repo: string | null;
  notifications: readonly ToastNotification[] | null;
  roles: readonly RoleHealth[];
}) {
  const [toasts, setToasts] = useState<ActiveToast[]>([]);
  const seen = useRef(new Set<string>());
  const journalReady = useRef(false);
  const previousHealth = useRef(new Map<string, boolean>());

  useEffect(() => {
    setToasts([]);
    seen.current = new Set();
    journalReady.current = false;
    previousHealth.current = new Map();
  }, [repo]);

  useEffect(() => {
    if (notifications === null) return;
    if (!journalReady.current) {
      seen.current = new Set(notifications.map((notification) => notification.id));
      journalReady.current = true;
      return;
    }
    const fresh: ToastNotification[] = [];
    for (const notification of notifications) {
      if (seen.current.has(notification.id)) continue;
      seen.current.add(notification.id);
      fresh.push(notification);
    }
    if (fresh.length === 0) return;
    setToasts((current) => [
      ...current,
      ...fresh.map(activeToast),
    ].slice(-RETAINED_TOASTS));
  }, [notifications]);

  useEffect(() => {
    const current = new Map(
      roles.flatMap((role) => (
        typeof role.alive === "boolean" ? [[role.role, role.alive] as const] : []
      )),
    );
    if (previousHealth.current.size > 0) {
      const changed: ToastNotification[] = [];
      for (const [role, alive] of current) {
        const previous = previousHealth.current.get(role);
        if (previous === undefined || previous === alive) continue;
        changed.push({
          id: `daemon:${repo ?? ""}:${role}:${alive}:${Date.now()}`,
          ts: new Date().toISOString(),
          severity: alive ? "info" : "attention",
          title: `${role} daemon ${alive ? "restarted" : "is down"}`,
          detail: alive ? "Harness polling resumed" : "Harness polling stopped",
          issue: null,
          pr: null,
        });
      }
      if (changed.length > 0) {
        setToasts((existing) => [
          ...existing,
          ...changed.map(activeToast),
        ].slice(-RETAINED_TOASTS));
      }
    }
    previousHealth.current = current;
  }, [repo, roles]);

  useEffect(() => {
    if (toasts.length === 0) return;
    const nextExpiry = Math.min(...toasts.map((toast) => toast.expiresAt));
    const timeout = window.setTimeout(() => {
      const now = Date.now();
      setToasts((current) => current.filter((toast) => toast.expiresAt > now));
    }, Math.max(0, nextExpiry - Date.now()));
    return () => window.clearTimeout(timeout);
  }, [toasts]);

  if (toasts.length === 0) return null;
  const overflow = Math.max(0, toasts.length - VISIBLE_TOASTS);
  const visible = toasts.slice(-VISIBLE_TOASTS);
  return (
    <aside className="toast-stack" aria-label="Notifications" aria-live="polite">
      {overflow > 0 ? <div className="toast-overflow">+{overflow} more</div> : null}
      {visible.map((toast) => (
        <div
          className={`toast toast-${toast.severity}`}
          key={toast.id}
          role={toast.severity === "attention" ? "alert" : "status"}
        >
          <span className="toast-mark" aria-hidden="true">
            {toast.severity === "attention" ? "!" : "✓"}
          </span>
          <span className="toast-copy">
            <strong>{toast.title}</strong>
            {toast.detail ? <span>{toast.detail}</span> : null}
          </span>
          <button
            className="toast-dismiss"
            type="button"
            aria-label={`Dismiss ${toast.title}`}
            onClick={() => {
              setToasts((current) => current.filter((item) => item.id !== toast.id));
            }}
          >
            ×
          </button>
        </div>
      ))}
    </aside>
  );
}
