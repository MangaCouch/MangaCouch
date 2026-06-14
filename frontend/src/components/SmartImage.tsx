// SmartImage — the robustness primitive for "图不能裂 (pages must never break)".
//
// Behavior:
//  - Shows a skeleton placeholder until the image loads.
//  - On load error, shows a retry button AND auto-retries with exponential
//    backoff (capped), appending a cache-busting param so a transiently-bad
//    response isn't served from cache.
//  - `forceLoad` (driven by the reader's "Load all images" action) immediately
//    (re)attempts a load even if the image is lazy/idle.
//  - Recovers from partially-decoded images: a zero-natural-size load is
//    treated as a failure and retried.

import {
  useCallback,
  useEffect,
  useRef,
  useState,
  type CSSProperties,
} from 'react';
import { t } from '../i18n/strings';

interface SmartImageProps {
  src: string;
  alt: string;
  className?: string;
  style?: CSSProperties;
  /** Force a (re)load attempt regardless of lazy/idle state. */
  forceLoad?: boolean;
  /** Max automatic retries before requiring a manual tap. */
  maxAutoRetries?: number;
  /** Called once the image successfully decodes. */
  onLoaded?: (img: HTMLImageElement) => void;
  /** `eager` for the current page, `lazy` for preloaded neighbours. */
  loading?: 'eager' | 'lazy';
  /** `img` for normal display; passed through to the element. */
  decoding?: 'sync' | 'async' | 'auto';
}

type Status = 'idle' | 'loading' | 'loaded' | 'error';

const BASE_BACKOFF_MS = 800;
const MAX_BACKOFF_MS = 15000;

export function SmartImage({
  src,
  alt,
  className,
  style,
  forceLoad = false,
  maxAutoRetries = 4,
  onLoaded,
  loading = 'lazy',
  decoding = 'async',
}: SmartImageProps) {
  const hasSrc = src.length > 0;
  const [status, setStatus] = useState<Status>(hasSrc ? 'loading' : 'idle');
  const [attempt, setAttempt] = useState(0);
  const retryTimer = useRef<number | undefined>(undefined);
  const imgRef = useRef<HTMLImageElement | null>(null);

  // Reset when the source changes. An empty src is a deferred ("idle") state —
  // the page is outside the preload window and shows only a skeleton.
  useEffect(() => {
    setStatus(src.length > 0 ? 'loading' : 'idle');
    setAttempt(0);
    return () => window.clearTimeout(retryTimer.current);
  }, [src]);

  // Cache-busting suffix on retries so a bad cached body isn't reused.
  const effectiveSrc =
    attempt > 0 ? `${src}${src.includes('?') ? '&' : '?'}_r=${attempt}` : src;

  const scheduleRetry = useCallback(() => {
    if (attempt >= maxAutoRetries) {
      setStatus('error');
      return;
    }
    const delay = Math.min(BASE_BACKOFF_MS * 2 ** attempt, MAX_BACKOFF_MS);
    setStatus('error'); // shows the failed state during the wait
    window.clearTimeout(retryTimer.current);
    retryTimer.current = window.setTimeout(() => {
      setAttempt((a) => a + 1);
      setStatus('loading');
    }, delay);
  }, [attempt, maxAutoRetries]);

  const handleLoad = useCallback(() => {
    const img = imgRef.current;
    // A decoded-but-empty image (partial/corrupt) counts as a failure.
    if (img && (img.naturalWidth === 0 || img.naturalHeight === 0)) {
      scheduleRetry();
      return;
    }
    setStatus('loaded');
    if (img) onLoaded?.(img);
  }, [onLoaded, scheduleRetry]);

  const handleError = useCallback(() => {
    scheduleRetry();
  }, [scheduleRetry]);

  // Manual retry — resets the backoff window.
  const manualRetry = useCallback(() => {
    window.clearTimeout(retryTimer.current);
    setAttempt((a) => a + 1);
    setStatus('loading');
  }, []);

  // `forceLoad` nudges a stuck/idle image to (re)attempt.
  useEffect(() => {
    if (forceLoad && (status === 'idle' || status === 'error')) {
      manualRetry();
    }
  }, [forceLoad, status, manualRetry]);

  return (
    <div className={`smart-image ${className ?? ''}`} style={style} data-status={status}>
      {status !== 'loaded' && (
        <div className="smart-image__skeleton" aria-hidden={status !== 'loading'}>
          {status === 'loading' && <div className="smart-image__shimmer" />}
          {status === 'error' && (
            <div className="smart-image__error">
              <span className="smart-image__error-icon">⚠</span>
              <span className="smart-image__error-text">{t('reader.failed')}</span>
              <button
                type="button"
                className="btn btn--small"
                onClick={manualRetry}
              >
                {t('reader.retry')}
              </button>
            </div>
          )}
        </div>
      )}
      {hasSrc && status !== 'idle' && (
        <img
          ref={imgRef}
          src={effectiveSrc}
          alt={alt}
          loading={loading}
          decoding={decoding}
          onLoad={handleLoad}
          onError={handleError}
          className="smart-image__img"
          style={{ opacity: status === 'loaded' ? 1 : 0 }}
          draggable={false}
        />
      )}
    </div>
  );
}
