// Small shared UI primitives kept in one file to avoid a sprawl of tiny files.

import {
  useEffect,
  useRef,
  useState,
  type ReactNode,
} from 'react';
import { lsGet, lsSet } from '../lib/storage';

/**
 * Collapsible card section (Settings et al.). Open/closed state persists per
 * `id` in localStorage so the page remembers how the user left it.
 */
export function Collapsible({
  id,
  icon,
  title,
  subtitle,
  defaultOpen = false,
  actions,
  children,
}: {
  id: string;
  icon?: string;
  title: string;
  subtitle?: string;
  defaultOpen?: boolean;
  /** Optional right-aligned header content (badges, counts). */
  actions?: ReactNode;
  children: ReactNode;
}) {
  const [open, setOpen] = useState<boolean>(() =>
    lsGet<boolean>(`mc.ui.section.${id}`, defaultOpen),
  );
  const toggle = () => {
    setOpen((v) => {
      lsSet(`mc.ui.section.${id}`, !v);
      return !v;
    });
  };

  return (
    <section className={`collapse ${open ? 'collapse--open' : ''}`}>
      <button
        type="button"
        className="collapse__header"
        aria-expanded={open}
        onClick={toggle}
      >
        {icon && (
          <span className="collapse__icon" aria-hidden>
            {icon}
          </span>
        )}
        <span className="collapse__titles">
          <span className="collapse__title">{title}</span>
          {subtitle && <span className="collapse__subtitle">{subtitle}</span>}
        </span>
        {actions && <span className="collapse__actions">{actions}</span>}
        <svg
          className="collapse__chevron"
          width="16"
          height="16"
          viewBox="0 0 16 16"
          aria-hidden
        >
          <path
            d="M4 6l4 4 4-4"
            fill="none"
            stroke="currentColor"
            strokeWidth="1.8"
            strokeLinecap="round"
            strokeLinejoin="round"
          />
        </svg>
      </button>
      <div className="collapse__outer">
        <div className="collapse__inner">
          <div className="collapse__body">{children}</div>
        </div>
      </div>
    </section>
  );
}

export function Spinner({ label }: { label?: string }) {
  return (
    <div className="spinner" role="status" aria-live="polite">
      <div className="spinner__ring" />
      {label && <span className="spinner__label">{label}</span>}
    </div>
  );
}

export function ErrorBanner({
  error,
  onRetry,
}: {
  error: Error;
  onRetry?: () => void;
}) {
  return (
    <div className="error-banner" role="alert">
      <span>{error.message}</span>
      {onRetry && (
        <button type="button" className="btn btn--small" onClick={onRetry}>
          Retry
        </button>
      )}
    </div>
  );
}

/** Interactive star rating widget. `readOnly` renders without click handlers. */
export function StarRating({
  value,
  max = 5,
  onChange,
  readOnly = false,
  size = 22,
}: {
  value: number;
  max?: number;
  onChange?: (v: number) => void;
  readOnly?: boolean;
  size?: number;
}) {
  const [hover, setHover] = useState<number | null>(null);
  const display = hover ?? value;
  return (
    <div
      className={`stars ${readOnly ? 'stars--readonly' : ''}`}
      role={readOnly ? 'img' : 'radiogroup'}
      aria-label={`Rating: ${value} of ${max}`}
    >
      {Array.from({ length: max }, (_, i) => {
        const n = i + 1;
        const filled = n <= display;
        return (
          <button
            key={n}
            type="button"
            role={readOnly ? undefined : 'radio'}
            aria-checked={readOnly ? undefined : n === value}
            className={`star ${filled ? 'star--filled' : ''}`}
            style={{ fontSize: size }}
            disabled={readOnly}
            onMouseEnter={() => !readOnly && setHover(n)}
            onMouseLeave={() => !readOnly && setHover(null)}
            onClick={() => !readOnly && onChange?.(n === value ? 0 : n)}
            aria-label={`${n} star${n > 1 ? 's' : ''}`}
          >
            ★
          </button>
        );
      })}
    </div>
  );
}

export function TagChip({
  label,
  title,
  onClick,
  active = false,
}: {
  label: string;
  title?: string;
  onClick?: () => void;
  active?: boolean;
}) {
  return (
    <button
      type="button"
      className={`chip ${active ? 'chip--active' : ''} ${onClick ? 'chip--clickable' : ''}`}
      title={title ?? label}
      onClick={onClick}
      disabled={!onClick}
    >
      {label}
    </button>
  );
}

export function Modal({
  open,
  onClose,
  title,
  children,
  wide = false,
}: {
  open: boolean;
  onClose: () => void;
  title?: string;
  children: ReactNode;
  wide?: boolean;
}) {
  const dialogRef = useRef<HTMLDivElement | null>(null);
  const restoreFocusTo = useRef<HTMLElement | null>(null);

  useEffect(() => {
    if (!open) return;
    // Move focus into the dialog; restore it to the trigger on close.
    restoreFocusTo.current = document.activeElement as HTMLElement | null;
    dialogRef.current?.focus();
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') onClose();
    };
    window.addEventListener('keydown', onKey);
    return () => {
      window.removeEventListener('keydown', onKey);
      restoreFocusTo.current?.focus?.();
    };
  }, [open, onClose]);

  if (!open) return null;
  return (
    <div className="modal-overlay" onClick={onClose}>
      <div
        ref={dialogRef}
        className={`modal ${wide ? 'modal--wide' : ''}`}
        role="dialog"
        aria-modal="true"
        aria-label={title}
        tabIndex={-1}
        onClick={(e) => e.stopPropagation()}
      >
        {title && (
          <header className="modal__header">
            <h2>{title}</h2>
            <button type="button" className="btn btn--icon" onClick={onClose} aria-label="Close">
              ✕
            </button>
          </header>
        )}
        <div className="modal__body">{children}</div>
      </div>
    </div>
  );
}
