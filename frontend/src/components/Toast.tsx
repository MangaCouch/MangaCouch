// Global toast notifications. A module-level emitter lets any code call
// `toast.success(...)` / `toast.error(...)` without threading a context; the
// single <Toaster/> mounted in App renders and auto-dismisses them.

import { useEffect, useState } from 'react';

export type ToastKind = 'success' | 'error' | 'info';

export interface ToastItem {
  id: number;
  kind: ToastKind;
  message: string;
}

type Listener = (item: ToastItem) => void;

let nextId = 1;
const listeners = new Set<Listener>();

function emit(kind: ToastKind, message: string): void {
  const item: ToastItem = { id: nextId++, kind, message };
  for (const fn of listeners) fn(item);
}

// eslint-disable-next-line react-refresh/only-export-components -- deliberate singleton API
export const toast = {
  success: (message: string) => emit('success', message),
  error: (message: string) => emit('error', message),
  info: (message: string) => emit('info', message),
};

const AUTO_DISMISS_MS = 3500;
const ERROR_DISMISS_MS = 6000;

export function Toaster() {
  const [items, setItems] = useState<ToastItem[]>([]);

  useEffect(() => {
    const onToast: Listener = (item) => {
      setItems((prev) => [...prev.slice(-3), item]); // keep at most 4 on screen
      const ttl = item.kind === 'error' ? ERROR_DISMISS_MS : AUTO_DISMISS_MS;
      window.setTimeout(() => {
        setItems((prev) => prev.filter((x) => x.id !== item.id));
      }, ttl);
    };
    listeners.add(onToast);
    return () => {
      listeners.delete(onToast);
    };
  }, []);

  if (items.length === 0) return null;
  return (
    <div className="toaster" role="region" aria-live="polite" aria-label="Notifications">
      {items.map((item) => (
        <div
          key={item.id}
          className={`toast toast--${item.kind}`}
          role={item.kind === 'error' ? 'alert' : 'status'}
          onClick={() => setItems((prev) => prev.filter((x) => x.id !== item.id))}
        >
          <span className="toast__icon" aria-hidden>
            {item.kind === 'success' ? '✓' : item.kind === 'error' ? '✕' : 'ℹ'}
          </span>
          <span className="toast__msg">{item.message}</span>
        </div>
      ))}
    </div>
  );
}
