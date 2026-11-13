import { Toaster, toast } from "sonner";
import { useEffect, type ReactNode } from "react";
import { errorMessage } from "./api";
import type { Translate } from "./types";

export { toast };

/** A failure shown in place, announced to assistive technology as it appears. */
export function ErrorNotice({
  error,
  t,
  children,
}: {
  error: unknown;
  t: Translate;
  /** Follows the message, e.g. a retry button. */
  children?: ReactNode;
}) {
  if (!error) return null;
  return (
    <p className="notice error" role="alert">
      {errorMessage(error, t)}
      {children}
    </p>
  );
}

/**
 * Publishes the header's height as `--site-head-height`, so notices start just below it
 * whatever it holds: the safe-area inset it pads in, or a second row if it ever wraps.
 * The reader hides the header; the fallback then keeps notices below the reader's top bar.
 */
function useHeaderClearance() {
  useEffect(() => {
    const head = document.querySelector<HTMLElement>(".site-head");
    const root = document.documentElement;
    if (!head || typeof ResizeObserver === "undefined") return;
    const measure = () => {
      const height = head.getBoundingClientRect().height;
      if (height > 0)
        root.style.setProperty("--site-head-height", `${Math.ceil(height)}px`);
      else root.style.removeProperty("--site-head-height");
    };
    measure();
    const observer = new ResizeObserver(measure);
    observer.observe(head);
    return () => {
      observer.disconnect();
      root.style.removeProperty("--site-head-height");
    };
  }, []);
}

/** The distance from the viewport top to `gap` below the header. */
const belowHeader = (gap: number) =>
  `calc(var(--site-head-height, calc(env(safe-area-inset-top) + 76px)) + ${gap}px)`;

/**
 * Notices sit at the top-left, below the header bar. The header's controls must stay
 * clickable while a notice shows: a pointer resting on a toast pauses its timer, so a
 * covered control (once the top-right "设置" button) stayed unreachable until the toast was
 * dismissed by hand. A notice may briefly cover the page's back link. On phones it spans
 * the width between the safe-area insets.
 */
export function Notifications({ t }: { t: Translate }) {
  useHeaderClearance();
  return (
    <Toaster
      position="top-left"
      richColors
      closeButton
      visibleToasts={3}
      gap={10}
      duration={4500}
      offset={{
        top: belowHeader(12),
        left: "max(24px, env(safe-area-inset-left))",
      }}
      mobileOffset={{
        top: belowHeader(8),
        left: "max(12px, env(safe-area-inset-left))",
        right: "max(12px, env(safe-area-inset-right))",
      }}
      containerAriaLabel={t("通知", "Notifications")}
      toastOptions={{
        className: "app-toast",
        closeButtonAriaLabel: t("关闭通知", "Dismiss notification"),
      }}
    />
  );
}
