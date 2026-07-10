// Pinch-zoom + pan for the paged reader view (multi-finger gestures, item:
// two-finger pinch zooms, one finger pans while zoomed, double-tap toggles,
// ctrl+wheel zooms on desktop trackpads). Transform-based — the image element
// itself is untouched so fit modes keep working at scale 1.

import { useCallback, useMemo, useRef, useState, type CSSProperties } from 'react';

const MIN_SCALE = 1;
const MAX_SCALE = 5;
const DOUBLE_TAP_MS = 300;
const DOUBLE_TAP_SLOP_PX = 40;
const DOUBLE_TAP_SCALE = 2.5;

interface Transform {
  scale: number;
  tx: number;
  ty: number;
}

const IDENTITY: Transform = { scale: 1, tx: 0, ty: 0 };

function clampScale(s: number): number {
  return Math.min(MAX_SCALE, Math.max(MIN_SCALE, s));
}

function distance(a: React.Touch, b: React.Touch): number {
  return Math.hypot(a.clientX - b.clientX, a.clientY - b.clientY);
}

function midpoint(a: React.Touch, b: React.Touch): { x: number; y: number } {
  return { x: (a.clientX + b.clientX) / 2, y: (a.clientY + b.clientY) / 2 };
}

export interface ZoomState {
  /** Style to apply to the zoomable container. */
  style: CSSProperties;
  /** True while zoomed in — page-turn taps/swipes should be suppressed. */
  zoomed: boolean;
  reset: () => void;
  handlers: {
    onTouchStart: (e: React.TouchEvent) => void;
    onTouchMove: (e: React.TouchEvent) => void;
    onTouchEnd: (e: React.TouchEvent) => void;
    onWheel: (e: React.WheelEvent) => void;
    onDoubleClick: (e: React.MouseEvent) => void;
  };
}

export function useZoom(containerRef: React.RefObject<HTMLElement | null>): ZoomState {
  const [transform, setTransform] = useState<Transform>(IDENTITY);
  const pinch = useRef<{ startDist: number; startTransform: Transform } | null>(null);
  const pan = useRef<{ x: number; y: number } | null>(null);
  const lastTap = useRef<{ time: number; x: number; y: number } | null>(null);

  const zoomed = transform.scale > 1.01;

  const reset = useCallback(() => setTransform(IDENTITY), []);

  /** Keep the pan within sensible bounds so the page can't be flung off-screen. */
  const clampPan = useCallback(
    (t: Transform): Transform => {
      const el = containerRef.current;
      if (!el) return t;
      const maxX = (el.clientWidth * (t.scale - 1)) / 2;
      const maxY = (el.clientHeight * (t.scale - 1)) / 2;
      return {
        scale: t.scale,
        tx: Math.min(maxX, Math.max(-maxX, t.tx)),
        ty: Math.min(maxY, Math.max(-maxY, t.ty)),
      };
    },
    [containerRef],
  );

  const zoomAt = useCallback(
    (clientX: number, clientY: number, nextScale: number) => {
      const el = containerRef.current;
      setTransform((prev) => {
        const scale = clampScale(nextScale);
        if (!el) return { ...prev, scale };
        const box = el.getBoundingClientRect();
        // Keep the focal point stationary: solve for the translation that maps
        // the same content point to the same screen point at the new scale.
        const cx = clientX - box.left - box.width / 2;
        const cy = clientY - box.top - box.height / 2;
        const k = scale / prev.scale;
        return clampPan({
          scale,
          tx: cx - k * (cx - prev.tx),
          ty: cy - k * (cy - prev.ty),
        });
      });
    },
    [containerRef, clampPan],
  );

  const onTouchStart = useCallback(
    (e: React.TouchEvent) => {
      if (e.touches.length === 2) {
        // Entering a pinch — this is ours, not a page-turn swipe.
        e.stopPropagation();
        pinch.current = {
          startDist: distance(e.touches[0], e.touches[1]),
          startTransform: transform,
        };
        pan.current = null;
        return;
      }
      if (e.touches.length === 1) {
        const tch = e.touches[0];
        // Double-tap detection (mobile).
        const now = Date.now();
        const last = lastTap.current;
        lastTap.current = { time: now, x: tch.clientX, y: tch.clientY };
        if (
          last &&
          now - last.time < DOUBLE_TAP_MS &&
          Math.hypot(tch.clientX - last.x, tch.clientY - last.y) < DOUBLE_TAP_SLOP_PX
        ) {
          e.stopPropagation();
          lastTap.current = null;
          if (zoomed) reset();
          else zoomAt(tch.clientX, tch.clientY, DOUBLE_TAP_SCALE);
          return;
        }
        if (zoomed) {
          // One-finger pan while zoomed; keep it away from the swipe handler.
          e.stopPropagation();
          pan.current = { x: tch.clientX, y: tch.clientY };
        }
      }
    },
    [transform, zoomed, reset, zoomAt],
  );

  const onTouchMove = useCallback(
    (e: React.TouchEvent) => {
      if (pinch.current && e.touches.length === 2) {
        e.stopPropagation();
        e.preventDefault();
        const { startDist, startTransform } = pinch.current;
        const dist = distance(e.touches[0], e.touches[1]);
        const mid = midpoint(e.touches[0], e.touches[1]);
        const target = clampScale(startTransform.scale * (dist / Math.max(1, startDist)));
        zoomAt(mid.x, mid.y, target);
        return;
      }
      if (pan.current && e.touches.length === 1 && zoomed) {
        e.stopPropagation();
        const tch = e.touches[0];
        const dx = tch.clientX - pan.current.x;
        const dy = tch.clientY - pan.current.y;
        pan.current = { x: tch.clientX, y: tch.clientY };
        setTransform((prev) => clampPan({ ...prev, tx: prev.tx + dx, ty: prev.ty + dy }));
      }
    },
    [zoomed, zoomAt, clampPan],
  );

  const onTouchEnd = useCallback(
    (e: React.TouchEvent) => {
      if (pinch.current) {
        if (e.touches.length < 2) pinch.current = null;
        e.stopPropagation();
        // Snap back to identity when the pinch ends near 1× — avoids a stuck
        // barely-zoomed state where taps stop turning pages.
        setTransform((prev) => (prev.scale < 1.05 ? IDENTITY : prev));
        return;
      }
      if (pan.current) {
        e.stopPropagation();
        if (e.touches.length === 0) pan.current = null;
      }
    },
    [],
  );

  // Desktop: ctrl+wheel (trackpad pinch) zooms at the cursor.
  const onWheel = useCallback(
    (e: React.WheelEvent) => {
      if (!e.ctrlKey && !e.metaKey) return;
      e.preventDefault();
      setTransform((prev) => prev); // ensure a state pass even for tiny deltas
      const factor = Math.exp(-e.deltaY / 200);
      zoomAt(e.clientX, e.clientY, transform.scale * factor);
    },
    [transform.scale, zoomAt],
  );

  const onDoubleClick = useCallback(
    (e: React.MouseEvent) => {
      if (zoomed) reset();
      else zoomAt(e.clientX, e.clientY, DOUBLE_TAP_SCALE);
    },
    [zoomed, reset, zoomAt],
  );

  const style = useMemo<CSSProperties>(
    () => ({
      transform: `translate(${transform.tx}px, ${transform.ty}px) scale(${transform.scale})`,
      transition: pinch.current || pan.current ? 'none' : 'transform 0.15s ease-out',
      willChange: 'transform',
    }),
    [transform],
  );

  return {
    style,
    zoomed,
    reset,
    handlers: { onTouchStart, onTouchMove, onTouchEnd, onWheel, onDoubleClick },
  };
}
