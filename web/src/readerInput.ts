import { useEffect, useRef } from "react";
import type { PointerEvent as ReactPointerEvent } from "react";

/** Focus targets that own their keys; the reader ignores key presses aimed at them. */
const TEXT_TARGETS =
  'input,textarea,select,[contenteditable=true],dialog,[role="dialog"],[role="menu"],[role="listbox"]';
/** Controls that Space (and Enter) activate natively. */
const ACTIVATABLE = 'button,a[href],summary,[role="button"],[role="slider"]';

export type ReaderKey =
  | "escape"
  | "toggle-play"
  | "left"
  | "right"
  | "next"
  | "previous"
  | "first"
  | "last";

/** Maps a window key press to a reader command, or null when the page should keep it. */
export function readerKey(event: KeyboardEvent): ReaderKey | null {
  const target = event.target as HTMLElement | null;
  if (
    event.defaultPrevented ||
    event.isComposing ||
    event.altKey ||
    event.ctrlKey ||
    event.metaKey ||
    target?.closest?.(TEXT_TARGETS)
  )
    return null;
  if (event.key === "Escape") return "escape";
  if (event.code === "Space" || event.key === " ")
    return target?.closest?.(ACTIVATABLE) ? null : "toggle-play";
  switch (event.key) {
    case "ArrowLeft":
      return "left";
    case "ArrowRight":
      return "right";
    case "ArrowDown":
    case "PageDown":
      return "next";
    case "ArrowUp":
    case "PageUp":
      return "previous";
    case "Home":
      return "first";
    case "End":
      return "last";
  }
  return null;
}

/** Subscribes once to window key presses; the latest handler always runs. */
export function useReaderKeys(
  handler: (key: ReaderKey, event: KeyboardEvent) => void,
) {
  const latest = useRef(handler);
  latest.current = handler;
  useEffect(() => {
    const listener = (event: KeyboardEvent) => {
      const key = readerKey(event);
      if (key) latest.current(key, event);
    };
    window.addEventListener("keydown", listener);
    return () => window.removeEventListener("keydown", listener);
  }, []);
}

/** A horizontal swipe must travel this far (CSS pixels) to turn a page. */
export const SWIPE_DISTANCE = 50;

/**
 * Pointer handling for the page stage: single-pointer horizontal swipes call
 * `swipe(-1)` (finger moved left) or `swipe(1)`, and any movement or multi-touch
 * suppresses the click that would otherwise act as a tap.
 */
export function usePageGestures(swipe: (direction: -1 | 1) => void) {
  const gesture = useRef({ x: 0, y: 0, moved: false, multi: false });
  const latest = useRef(swipe);
  latest.current = swipe;
  return {
    /** True when the last pointer sequence moved, so its click is not a tap. */
    moved: () => gesture.current.moved,
    handlers: {
      onPointerDown: (event: ReactPointerEvent) => {
        // A primary pointer starts a new sequence; any further pointer makes it multi-touch.
        if (event.isPrimary)
          gesture.current = {
            x: event.clientX,
            y: event.clientY,
            moved: false,
            multi: false,
          };
        else gesture.current = { ...gesture.current, moved: true, multi: true };
      },
      onPointerMove: (event: ReactPointerEvent) => {
        const state = gesture.current;
        if (
          event.isPrimary &&
          Math.hypot(event.clientX - state.x, event.clientY - state.y) > 8
        )
          state.moved = true;
      },
      onPointerUp: (event: ReactPointerEvent) => {
        const state = gesture.current;
        if (!event.isPrimary || state.multi) return;
        const dx = event.clientX - state.x,
          dy = event.clientY - state.y;
        if (Math.abs(dx) > SWIPE_DISTANCE && Math.abs(dx) > Math.abs(dy))
          latest.current(dx < 0 ? -1 : 1);
      },
      onPointerCancel: () => {
        gesture.current.moved = true;
        gesture.current.multi = true;
      },
    },
  };
}
