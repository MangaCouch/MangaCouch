import { useEffect, useLayoutEffect, useRef, useState } from "react";
import { createPortal } from "react-dom";

/** Hover this long before the first hint shows. */
export const HINT_DELAY_MS = 450;
/** Within this long after a hint hides, the next one shows at once (moving along a toolbar). */
const WARM_MS = 400;
/** Gap between the element and its hint, and the hint's minimum margin to the window. */
const GAP = 8;
/** Elements that get a hint: labelled controls (icon buttons) and anything with a title. */
const TARGETS =
  "button[aria-label], [role='button'][aria-label], a[aria-label], [title], [data-hint]";

/**
 * Elements larger than this (px²) are hit areas, not controls (page-turn zones, the
 * click-outside scrim of a panel), and get no hint; `data-hint=""` opts out too.
 */
const MAX_AREA = 40_000;
/** What an element's hint says; nothing when its visible text already says it. */
export function hintText(element: Element) {
  const text = (
    element.getAttribute("data-hint") ??
    element.getAttribute("data-hint-title") ??
    element.getAttribute("title") ??
    element.getAttribute("aria-label") ??
    ""
  ).trim();
  return text === (element.textContent ?? "").trim() ? "" : text;
}

type Shown = { text: string; rect: DOMRect };

/**
 * One styled hover hint for the whole app, for mouse pointers only (touch shows none).
 * A `title` is held back while its element is hovered, so the browser's own tooltip
 * does not show as well.
 */
export function HoverHints() {
  const [shown, setShown] = useState<Shown | null>(null);
  const node = useRef<HTMLDivElement>(null);
  const [place, setPlace] = useState<{ left: number; top: number } | null>(
    null,
  );
  useEffect(() => {
    let target: Element | null = null;
    let timer: ReturnType<typeof setTimeout> | undefined;
    let hiddenAt = -Infinity;
    const release = () => {
      clearTimeout(timer);
      if (target) {
        const title = target.getAttribute("data-hint-title");
        target.removeAttribute("data-hint-title");
        // React may have set a newer title meanwhile; keep that one.
        if (title !== null && !target.hasAttribute("title"))
          target.setAttribute("title", title);
      }
      target = null;
      setShown((current) => {
        if (current) hiddenAt = performance.now();
        return null;
      });
    };
    const over = (event: PointerEvent) => {
      if (event.pointerType !== "mouse") return;
      const element = (event.target as Element | null)?.closest?.(TARGETS);
      if (element === target) return;
      release();
      if (!element || !hintText(element)) return;
      const { width, height } = element.getBoundingClientRect();
      if (width * height > MAX_AREA) return;
      target = element;
      const title = element.getAttribute("title");
      if (title !== null) {
        element.setAttribute("data-hint-title", title);
        element.removeAttribute("title");
      }
      const show = () => {
        if (target !== element || !element.isConnected) return;
        setShown({
          text: hintText(element),
          rect: element.getBoundingClientRect(),
        });
      };
      if (performance.now() - hiddenAt < WARM_MS) show();
      else timer = setTimeout(show, HINT_DELAY_MS);
    };
    const out = (event: PointerEvent) => {
      if (
        target &&
        !(
          event.relatedTarget instanceof Node &&
          target.contains(event.relatedTarget)
        )
      )
        release();
    };
    const dismiss = () => {
      if (target) release();
    };
    document.addEventListener("pointerover", over, true);
    document.addEventListener("pointerout", out, true);
    for (const name of ["pointerdown", "keydown", "wheel"] as const)
      document.addEventListener(name, dismiss, true);
    window.addEventListener("scroll", dismiss, true);
    window.addEventListener("blur", dismiss);
    return () => {
      release();
      document.removeEventListener("pointerover", over, true);
      document.removeEventListener("pointerout", out, true);
      for (const name of ["pointerdown", "keydown", "wheel"] as const)
        document.removeEventListener(name, dismiss, true);
      window.removeEventListener("scroll", dismiss, true);
      window.removeEventListener("blur", dismiss);
    };
  }, []);
  // Below the element, or above it near the bottom edge; kept inside the window.
  useLayoutEffect(() => {
    if (!shown || !node.current) return setPlace(null);
    const { width, height } = node.current.getBoundingClientRect();
    const { rect } = shown;
    const below = rect.bottom + GAP + height <= window.innerHeight - GAP;
    setPlace({
      left: Math.min(
        Math.max(GAP, rect.left + rect.width / 2 - width / 2),
        window.innerWidth - GAP - width,
      ),
      top: below ? rect.bottom + GAP : Math.max(GAP, rect.top - GAP - height),
    });
  }, [shown]);
  if (!shown) return null;
  return createPortal(
    <div
      ref={node}
      className="hover-hint"
      aria-hidden="true"
      style={
        place
          ? { left: place.left, top: place.top }
          : { left: 0, top: 0, visibility: "hidden" }
      }
    >
      {shown.text}
    </div>,
    document.body,
  );
}
