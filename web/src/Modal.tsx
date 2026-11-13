import { useState, type ReactNode } from "react";
import { Dialog } from "radix-ui";
import { X } from "lucide-react";

export function Modal({
  title,
  close,
  closeLabel,
  children,
  busy = false,
  dismissOutside = true,
}: {
  title: string;
  close: () => void;
  closeLabel: string;
  children: ReactNode;
  busy?: boolean;
  dismissOutside?: boolean;
}) {
  const [returnFocus] = useState(() => {
    const active = document.activeElement as HTMLElement | null;
    const menu = active?.closest('[role="menu"]');
    const triggerId =
      menu?.getAttribute("data-return-focus") ??
      menu?.getAttribute("aria-labelledby");
    const trigger = triggerId ? document.getElementById(triggerId) : null;
    return trigger
      ? trigger.matches("button,a[href],[tabindex='0']")
        ? trigger
        : trigger.querySelector<HTMLElement>("button,a[href],[tabindex='0']")
      : active;
  });
  /** Escape inside a busy dialog or a local combobox does not close it. */
  const keepOpen = (event: { target: EventTarget | null }) =>
    busy ||
    !!(event.target as HTMLElement | null)?.closest?.(
      '[data-escape-local="true"]',
    );
  return (
    <Dialog.Root
      open
      onOpenChange={(open) => {
        if (!open && !busy) close();
      }}
    >
      <Dialog.Portal>
        <Dialog.Overlay className="modal-overlay" />
        <Dialog.Content
          className="modal"
          aria-describedby={undefined}
          // Focus starts on the first field; a dialog without one focuses itself, so Tab
          // reaches its controls in order and no button wears a ring it was not given.
          onOpenAutoFocus={(event) => {
            event.preventDefault();
            const content = event.currentTarget as HTMLElement;
            (
              content.querySelector<HTMLElement>(
                "input:not([type=hidden],[type=checkbox],:disabled),textarea:not(:disabled),[role=combobox]:not(:disabled)",
              ) ?? content
            ).focus({ preventScroll: true });
          }}
          onCloseAutoFocus={(event) => {
            event.preventDefault();
            if (returnFocus?.isConnected) returnFocus.focus();
          }}
          // Radix handles Escape and outside clicks and then calls onOpenChange(false);
          // these only veto a dismissal.
          onEscapeKeyDown={(event) => {
            if (keepOpen(event)) event.preventDefault();
          }}
          // Radix registers its Escape listener one render after the dialog mounts,
          // so a key pressed right after opening (e.g. from a closing menu) would be
          // lost. The bubbling fallback only acts when Radix did not handle the key.
          onKeyDown={(event) => {
            if (
              event.key === "Escape" &&
              !event.defaultPrevented &&
              !keepOpen(event)
            ) {
              event.preventDefault();
              close();
            }
          }}
          onInteractOutside={(event) => {
            if (busy || !dismissOutside) event.preventDefault();
          }}
        >
          <header className="modal-head">
            <Dialog.Title asChild>
              <h2>{title}</h2>
            </Dialog.Title>
            <Dialog.Close asChild>
              <button
                type="button"
                className="icon-button close-button"
                disabled={busy}
                aria-label={closeLabel}
              >
                <X size={16} />
              </button>
            </Dialog.Close>
          </header>
          {children}
        </Dialog.Content>
      </Dialog.Portal>
    </Dialog.Root>
  );
}
