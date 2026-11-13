import { useEffect, useLayoutEffect, useRef, useState } from "react";
import { storageKey } from "./device";

function read<T>(key: string, initial: T): T {
  try {
    const value: unknown = JSON.parse(sessionStorage.getItem(key) ?? "null");
    if (Array.isArray(initial)) {
      if (
        Array.isArray(value) &&
        value.length > 0 &&
        value.length < 10000 &&
        value.every((item) => typeof item === "string")
      )
        return value as T;
    } else if (
      typeof value === typeof initial &&
      value !== null &&
      (typeof value !== "number" ||
        (Number.isSafeInteger(value) && value >= Number(initial)))
    )
      return value as T;
  } catch {
    /* View persistence is optional. */
  }
  return initial;
}

/** This tab's position survives a trip to a book without preserving stale server results. */
export function useViewState<T>(name: string, initial: T) {
  const key = storageKey(`view:${name}`);
  const [value, setValue] = useState<T>(() => read(key, initial));
  useEffect(() => {
    try {
      sessionStorage.setItem(key, JSON.stringify(value));
    } catch {
      /* Optional storage. */
    }
  }, [key, value]);
  return [value, setValue] as const;
}

/** Briefly retry a clamped restoration while content arrives, then let the user own scrolling. */
export function restoreScroll(y: number, settled: () => void = () => {}) {
  let stopped = false;
  let observer: ResizeObserver | null = null;
  let timer: ReturnType<typeof setTimeout> | undefined;
  const events = ["wheel", "touchstart", "keydown", "pointerdown"] as const;
  const stop = (notify = true) => {
    if (stopped) return;
    stopped = true;
    observer?.disconnect();
    clearTimeout(timer);
    for (const event of events) window.removeEventListener(event, interrupt);
    if (notify) settled();
  };
  const interrupt = () => stop();
  const restore = () => {
    if (stopped) return;
    window.scrollTo?.(0, Math.max(0, y));
    if (
      y === 0 ||
      document.documentElement.scrollHeight - window.innerHeight >= y
    )
      stop();
  };
  if (typeof ResizeObserver !== "undefined") {
    observer = new ResizeObserver(restore);
    observer.observe(document.body);
  }
  for (const event of events)
    window.addEventListener(event, interrupt, { once: true });
  timer = setTimeout(stop, 1500);
  restore();
  return () => stop(false);
}

export function useViewScroll(name: string, ready: boolean) {
  const key = storageKey(`scroll:${name}`);
  const route = location.hash;
  const restored = useRef(false);
  useLayoutEffect(() => {
    restored.current = false;
    if (!ready) return;
    const save = () => {
      if (!restored.current || location.hash !== route) return;
      try {
        sessionStorage.setItem(key, String(Math.round(window.scrollY)));
      } catch {
        /* Optional storage. */
      }
    };
    const stop = restoreScroll(read(key, 0), () => {
      restored.current = true;
      save();
    });
    window.addEventListener("scroll", save);
    return () => {
      stop();
      window.removeEventListener("scroll", save);
    };
  }, [ready, key, route]);
}
