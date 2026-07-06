// The Reader route — the centerpiece. Wires the reader state to the paged /
// scroll views, a top toolbar, a settings panel, bookmarks, fullscreen,
// autoplay, keyboard navigation, and touch swipe.

import {
  useCallback,
  useEffect,
  useRef,
  useState,
  type TouchEvent as ReactTouchEvent,
} from 'react';
import { useNavigate, useParams, useSearchParams } from 'react-router-dom';
import { useReader } from '../reader/useReader';
import { PagedView } from '../reader/PagedView';
import { ScrollView } from '../reader/ScrollView';
import { ReaderSettingsPanel } from '../reader/ReaderSettingsPanel';
import { Spinner, ErrorBanner, Modal } from '../components/ui';
import {
  addBookmark,
  bookmarksFor,
  isBookmarked,
  removeBookmark,
  type Bookmark,
} from '../reader/settings';
import { t } from '../i18n/strings';

const SWIPE_THRESHOLD = 50;

export function Reader() {
  const { id = '' } = useParams();
  const [params] = useSearchParams();
  const navigate = useNavigate();
  const initialPage = params.has('page') ? Number(params.get('page')) : undefined;

  const reader = useReader(id, initialPage);
  const {
    archive,
    loading,
    error,
    current,
    total,
    settings,
    next,
    prev,
    goToPage,
    atStart,
    atEnd,
    triggerLoadAll,
    forceLoadAll,
  } = reader;

  const [chromeVisible, setChromeVisible] = useState(true);
  const [settingsOpen, setSettingsOpen] = useState(false);
  const [bookmarksOpen, setBookmarksOpen] = useState(false);
  const [isFullscreen, setIsFullscreen] = useState(false);
  const [bookmarked, setBookmarked] = useState(false);
  const rootRef = useRef<HTMLDivElement | null>(null);
  const touchStart = useRef<{ x: number; y: number } | null>(null);

  // Track bookmark state for the current page.
  useEffect(() => {
    setBookmarked(isBookmarked(id, current));
  }, [id, current]);

  // Keyboard navigation. Direction-aware arrow keys for paged mode.
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      // Don't fight the modals, browser shortcuts (Cmd/Ctrl+F…), or focused
      // form controls (e.g. arrow keys on the page slider / settings sliders).
      if (settingsOpen || bookmarksOpen) return;
      if (e.ctrlKey || e.metaKey || e.altKey) return;
      const target = e.target as HTMLElement | null;
      if (target?.closest('input, textarea, select')) return;
      // Space on a focused button would both click it and page-advance.
      if (e.key === ' ' && target?.closest('button')) return;
      switch (e.key) {
        case 'ArrowRight':
          if (settings.direction === 'rtl') prev();
          else next();
          break;
        case 'ArrowLeft':
          if (settings.direction === 'rtl') next();
          else prev();
          break;
        case 'ArrowDown':
        case ' ':
          if (settings.mode === 'paged') {
            e.preventDefault();
            next();
          }
          break;
        case 'ArrowUp':
          if (settings.mode === 'paged') {
            e.preventDefault();
            prev();
          }
          break;
        case 'f':
          toggleFullscreen();
          break;
        case 'Escape':
          if (!settingsOpen && !bookmarksOpen) navigate(-1);
          break;
        default:
          break;
      }
    };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [next, prev, settings.direction, settings.mode, settingsOpen, bookmarksOpen]);

  // Autoplay timer.
  useEffect(() => {
    if (settings.autoplaySeconds <= 0 || settings.mode !== 'paged') return;
    if (atEnd) return;
    const timer = window.setInterval(() => next(), settings.autoplaySeconds * 1000);
    return () => window.clearInterval(timer);
  }, [settings.autoplaySeconds, settings.mode, next, atEnd, current]);

  // Reflect browser fullscreen state.
  useEffect(() => {
    const onFs = () => setIsFullscreen(!!document.fullscreenElement);
    document.addEventListener('fullscreenchange', onFs);
    return () => document.removeEventListener('fullscreenchange', onFs);
  }, []);

  const toggleFullscreen = useCallback(() => {
    if (document.fullscreenElement) {
      document.exitFullscreen().catch(() => {});
    } else {
      rootRef.current?.requestFullscreen().catch(() => {});
    }
  }, []);

  // Touch swipe (paged mode only; scroll mode uses native scrolling).
  const onTouchStart = useCallback((e: ReactTouchEvent) => {
    // Dragging the page slider or toolbar buttons is not a page-turn gesture.
    const target = e.target as HTMLElement | null;
    if (target?.closest('input, button, .reader-bar, .reader-foot')) {
      touchStart.current = null;
      return;
    }
    const tch = e.touches[0];
    touchStart.current = { x: tch.clientX, y: tch.clientY };
  }, []);

  const onTouchEnd = useCallback(
    (e: ReactTouchEvent) => {
      if (settings.mode !== 'paged' || !touchStart.current) return;
      const tch = e.changedTouches[0];
      const dx = tch.clientX - touchStart.current.x;
      const dy = tch.clientY - touchStart.current.y;
      touchStart.current = null;
      if (Math.abs(dx) < SWIPE_THRESHOLD || Math.abs(dx) < Math.abs(dy)) return;
      // Swipe right -> previous in LTR; direction-aware.
      const swipedRight = dx > 0;
      if (settings.direction === 'rtl') {
        if (swipedRight) next();
        else prev();
      } else {
        if (swipedRight) prev();
        else next();
      }
    },
    [settings.mode, settings.direction, next, prev],
  );

  const toggleBookmark = useCallback(() => {
    if (isBookmarked(id, current)) {
      removeBookmark(id, current);
      setBookmarked(false);
    } else {
      const b: Bookmark = {
        archiveId: id,
        page: current,
        createdAt: Date.now(),
        title: archive?.title,
      };
      addBookmark(b);
      setBookmarked(true);
    }
  }, [id, current, archive?.title]);

  if (loading) {
    return (
      <div className="reader reader--loading">
        <Spinner label={t('reader.loading')} />
      </div>
    );
  }
  if (error) {
    return (
      <div className="reader reader--loading">
        <ErrorBanner error={error} onRetry={() => navigate(0)} />
      </div>
    );
  }

  return (
    <div
      ref={rootRef}
      className={`reader reader--${settings.mode} ${chromeVisible ? '' : 'reader--immersive'}`}
      onTouchStart={onTouchStart}
      onTouchEnd={onTouchEnd}
    >
      {/* Top toolbar */}
      <header className="reader-bar">
        <button
          type="button"
          className="btn btn--icon"
          onClick={() => navigate(-1)}
          aria-label={t('reader.close')}
          title={t('common.back')}
        >
          ←
        </button>
        <div className="reader-bar__title" title={archive?.title}>
          {archive?.title}
        </div>
        <div className="reader-bar__spacer" />
        <span className="reader-bar__counter">
          {current + 1} / {total}
        </span>
        <button
          type="button"
          className={`btn btn--icon ${bookmarked ? 'btn--active' : ''}`}
          onClick={toggleBookmark}
          aria-label={t('reader.bookmark')}
          title={t('reader.bookmark')}
        >
          {bookmarked ? '🔖' : '📑'}
        </button>
        <button
          type="button"
          className="btn btn--icon"
          onClick={() => setBookmarksOpen(true)}
          aria-label={t('reader.bookmarks')}
          title={t('reader.bookmarks')}
        >
          📚
        </button>
        <button
          type="button"
          className="btn btn--icon"
          onClick={toggleFullscreen}
          aria-label={t('reader.fullscreen')}
          title={t('reader.fullscreen')}
        >
          {isFullscreen ? '🗗' : '⛶'}
        </button>
        <button
          type="button"
          className="btn btn--icon"
          onClick={() => setSettingsOpen(true)}
          aria-label={t('reader.settings')}
          title={t('reader.settings')}
        >
          ⚙
        </button>
      </header>

      {/* Viewport */}
      <div
        className="reader-viewport"
        onClick={(e) => {
          // Center tap toggles chrome; in paged mode the edge zones handle
          // paging, so only taps outside them toggle.
          const target = e.target as HTMLElement;
          if (settings.mode === 'scroll') {
            if (target.closest('.scroll__img') || target.classList.contains('scroll'))
              setChromeVisible((v) => !v);
          } else if (!target.closest('.pages__zone')) {
            setChromeVisible((v) => !v);
          }
        }}
      >
        {settings.mode === 'paged' ? (
          <PagedView reader={reader} archiveId={id} />
        ) : (
          <ScrollView reader={reader} archiveId={id} />
        )}
      </div>

      {/* Bottom bar: page slider + load-all */}
      <footer className="reader-foot">
        <button
          type="button"
          className="btn btn--small"
          onClick={prev}
          disabled={atStart}
        >
          ‹
        </button>
        <input
          type="range"
          className="reader-foot__slider"
          min={0}
          max={Math.max(0, total - 1)}
          value={current}
          // For RTL the slider is mirrored so dragging right advances forward.
          dir={settings.direction === 'rtl' ? 'rtl' : 'ltr'}
          onChange={(e) => goToPage(Number(e.target.value))}
          aria-label={t('reader.page')}
        />
        <button type="button" className="btn btn--small" onClick={next} disabled={atEnd}>
          ›
        </button>
        <button
          type="button"
          className={`btn btn--small ${forceLoadAll ? 'btn--active' : ''}`}
          onClick={triggerLoadAll}
          title={t('reader.loadAll')}
        >
          ⤓ {t('reader.loadAll')}
        </button>
      </footer>

      <Modal
        open={settingsOpen}
        onClose={() => setSettingsOpen(false)}
        title={t('reader.settings')}
      >
        <ReaderSettingsPanel reader={reader} />
      </Modal>

      <Modal
        open={bookmarksOpen}
        onClose={() => setBookmarksOpen(false)}
        title={t('reader.bookmarks')}
      >
        <BookmarkList
          archiveId={id}
          onGo={(page) => {
            goToPage(page);
            setBookmarksOpen(false);
          }}
        />
      </Modal>
    </div>
  );
}

function BookmarkList({
  archiveId,
  onGo,
}: {
  archiveId: string;
  onGo: (page: number) => void;
}) {
  const [marks, setMarks] = useState<Bookmark[]>(() => bookmarksFor(archiveId));
  const refresh = () => setMarks(bookmarksFor(archiveId));
  if (marks.length === 0) return <p className="detail__muted">—</p>;
  return (
    <ul className="bookmark-list">
      {marks
        .sort((a, b) => a.page - b.page)
        .map((b) => (
          <li key={b.page} className="bookmark-list__item">
            <button type="button" className="bookmark-list__go" onClick={() => onGo(b.page)}>
              {t('reader.page')} {b.page + 1}
            </button>
            <button
              type="button"
              className="btn btn--icon btn--small"
              onClick={() => {
                removeBookmark(archiveId, b.page);
                refresh();
              }}
              aria-label="Remove bookmark"
            >
              ✕
            </button>
          </li>
        ))}
    </ul>
  );
}
