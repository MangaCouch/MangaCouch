import { useEffect, useRef } from "react";
import type {
  PointerEvent as ReactPointerEvent,
  RefObject,
  TouchEvent as ReactTouchEvent,
} from "react";

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
function readerKey(event: KeyboardEvent): ReaderKey | null {
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

/** One ↓/↑ (PageDown/PageUp) press scrolls a tall page by this share of the screen. */
const KEY_SCROLL_SHARE = 0.85;
/** Scroll offsets this close to an edge count as at the edge. */
const EDGE_SLACK = 2;
/**
 * Scrolls a paged canvas that is taller than the screen one step down (`1`) or up
 * (`-1`), stopping at its edge. Returns false when it was already at that edge (or
 * fits the screen), so the key turns the page instead.
 */
export function scrollWithin(canvas: HTMLElement, way: 1 | -1): boolean {
  const end = canvas.scrollHeight - canvas.clientHeight;
  const top = canvas.scrollTop;
  if (end <= EDGE_SLACK) return false;
  if (way === 1 ? top >= end - EDGE_SLACK : top <= EDGE_SLACK) return false;
  const step = Math.max(1, Math.round(canvas.clientHeight * KEY_SCROLL_SHARE));
  canvas.scrollTop = Math.max(0, Math.min(end, top + way * step));
  return true;
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
const SWIPE_DISTANCE = 50;

/**
 * Swipe handling for the page stage. One-finger horizontal swipes call `swipe(-1)`
 * (finger moved left) or `swipe(1)`; any movement or multi-touch suppresses the
 * click that would otherwise act as a tap. Touch swipes are read from touch events,
 * which keep arriving while the browser pans the page (pointer events are cancelled
 * then); mouse and pen drags use pointer events.
 */
export function usePageGestures(swipe: (direction: -1 | 1) => void) {
  const gesture = useRef({ x: 0, y: 0, moved: false, multi: false });
  const finger = useRef({ x: 0, y: 0, multi: false, active: false });
  const latest = useRef(swipe);
  latest.current = swipe;
  const decide = (dx: number, dy: number) => {
    if (Math.abs(dx) > SWIPE_DISTANCE && Math.abs(dx) > Math.abs(dy))
      latest.current(dx < 0 ? -1 : 1);
  };
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
        if (!event.isPrimary || state.multi || event.pointerType === "touch")
          return;
        decide(event.clientX - state.x, event.clientY - state.y);
      },
      onPointerCancel: () => {
        gesture.current.moved = true;
        gesture.current.multi = true;
      },
      onTouchStart: (event: ReactTouchEvent) => {
        const first = event.touches[0];
        if (event.touches.length === 1 && first)
          finger.current = {
            x: first.clientX,
            y: first.clientY,
            multi: false,
            active: true,
          };
        else finger.current.multi = true;
      },
      onTouchEnd: (event: ReactTouchEvent) => {
        const state = finger.current,
          last = event.changedTouches[0];
        if (event.touches.length || !state.active) return;
        state.active = false;
        if (state.multi || !last) return;
        decide(last.clientX - state.x, last.clientY - state.y);
      },
      onTouchCancel: () => {
        finger.current.active = false;
      },
    },
  };
}

/** Largest pinch zoom of the pages, as a multiple of the fitted size. */
const MAX_ZOOM = 4;
/** A pinch released below this zoom snaps back to the fitted size. */
const SNAP_ZOOM = 1.08;
export type ZoomFocus = { x: number; y: number };

/**
 * Two-finger pinch on `target` zooms the pages only. The browser's own pinch zoom,
 * which would also enlarge the toolbar and scrubber, is suppressed while two fingers
 * are down; one finger keeps scrolling natively. `change` receives the new zoom and the
 * pinch midpoint in client coordinates, so the view can keep that point in place.
 */
export function usePinchZoom(
  target: RefObject<HTMLElement | null>,
  zoom: number,
  change: (zoom: number, focus: ZoomFocus) => void,
) {
  const latest = useRef({ zoom, change });
  latest.current = { zoom, change };
  useEffect(() => {
    const node = target.current;
    if (!node) return;
    let start: { distance: number; zoom: number } | null = null;
    let focus: ZoomFocus = { x: 0, y: 0 };
    let frame = 0,
      scheduled = false,
      pending: number | null = null,
      shown = latest.current.zoom;
    const measure = (touches: TouchList) => {
      const a = touches[0]!,
        b = touches[1]!;
      return {
        distance: Math.hypot(a.clientX - b.clientX, a.clientY - b.clientY),
        focus: {
          x: (a.clientX + b.clientX) / 2,
          y: (a.clientY + b.clientY) / 2,
        },
      };
    };
    const flush = () => {
      scheduled = false;
      if (pending !== null) {
        shown = pending;
        latest.current.change(pending, focus);
      }
      pending = null;
    };
    const begin = (event: TouchEvent) => {
      if (event.touches.length !== 2) return;
      const value = measure(event.touches);
      shown = latest.current.zoom;
      start = { distance: Math.max(1, value.distance), zoom: shown };
      focus = value.focus;
    };
    const move = (event: TouchEvent) => {
      if (!start || event.touches.length !== 2) return;
      event.preventDefault();
      const value = measure(event.touches);
      focus = value.focus;
      pending = Math.min(
        MAX_ZOOM,
        Math.max(1, (start.zoom * value.distance) / start.distance),
      );
      if (!scheduled) {
        scheduled = true;
        frame = requestAnimationFrame(flush);
      }
    };
    const end = (event: TouchEvent) => {
      if (!start || event.touches.length >= 2) return;
      start = null;
      if (scheduled) {
        cancelAnimationFrame(frame);
        flush();
      }
      if (shown > 1 && shown < SNAP_ZOOM) latest.current.change(1, focus);
    };
    // Safari zooms the page from its own gesture events unless they are cancelled.
    const gesture = (event: Event) => event.preventDefault();
    const active = { passive: false } as const;
    node.addEventListener("touchstart", begin, active);
    node.addEventListener("touchmove", move, active);
    node.addEventListener("touchend", end);
    node.addEventListener("touchcancel", end);
    node.addEventListener("gesturestart", gesture, active);
    node.addEventListener("gesturechange", gesture, active);
    return () => {
      if (scheduled) cancelAnimationFrame(frame);
      node.removeEventListener("touchstart", begin);
      node.removeEventListener("touchmove", move);
      node.removeEventListener("touchend", end);
      node.removeEventListener("touchcancel", end);
      node.removeEventListener("gesturestart", gesture);
      node.removeEventListener("gesturechange", gesture);
    };
  }, [target]);
}

/**
 * Scroll offset that keeps the content point under `focus` (a client coordinate,
 * measured from `edge`) in place when content scaled from its origin goes from
 * `before` to `after` times its size.
 */
export function zoomedOffset(
  offset: number,
  focus: number,
  edge: number,
  before: number,
  after: number,
) {
  const point = focus - edge;
  return Math.max(0, ((offset + point) * after) / before - point);
}
