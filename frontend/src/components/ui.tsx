// Small shared UI primitives kept in one file to avoid a sprawl of tiny files.

import {
  useEffect,
  useState,
  type ReactNode,
} from 'react';

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
      role={readOnly ? undefined : 'slider'}
      aria-valuenow={value}
      aria-valuemin={0}
      aria-valuemax={max}
    >
      {Array.from({ length: max }, (_, i) => {
        const n = i + 1;
        const filled = n <= display;
        return (
          <button
            key={n}
            type="button"
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
  useEffect(() => {
    if (!open) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') onClose();
    };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, [open, onClose]);

  if (!open) return null;
  return (
    <div className="modal-overlay" onClick={onClose}>
      <div
        className={`modal ${wide ? 'modal--wide' : ''}`}
        role="dialog"
        aria-modal="true"
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
