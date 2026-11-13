import {
  useFullscreen,
  useReaderLifecycle,
  useReadingProgress,
} from "./readerLifecycle";
import { ContextActions } from "./Controls";
import { useEffect, useLayoutEffect, useMemo, useRef, useState } from "react";
import {
  ArrowLeft,
  ChevronLeft,
  ChevronRight,
  Pause,
  Settings2,
  Maximize,
  Minimize,
  Timer,
} from "lucide-react";
import { Modal } from "./Modal";
import { Pill } from "./Pills";
import { useWarmPages } from "./preload";
import {
  Autoplay,
  adjacent,
  pageRatios,
  readingStatus,
  startingPage,
  spreads,
  widePages,
} from "./reading";
import type { ReadingMode } from "./reading";
import { PagedView } from "./PagedView";
import { ScrollView } from "./ScrollView";
import type { ScrollHandle } from "./ScrollView";
import type { ViewState } from "./PageImage";
import type { PersonalQueue, SaveStatus } from "./personal";
import type { Pages } from "./pages";
import { useBookTitle } from "./PreferenceContext";
import { readDevice, remember } from "./device";
import type { ReadingDirection, ReadingFit } from "./device";
import { ReaderSettings } from "./ReaderSettings";
import type { ReaderKey } from "./readerInput";
import { ReaderScrubber } from "./ReaderScrubber";
import { SaveFailure } from "./SaveFailure";
import {
  scrollWithin,
  usePageGestures,
  usePinchZoom,
  useReaderKeys,
} from "./readerInput";
import type { ZoomFocus } from "./readerInput";
import type { Book, Translate, CollectionNavigation } from "./types";

/** Page turns closer together than this count as rapid flipping (no full loads). */
const RAPID_TURN_MS = 180;

export function Reader({
  book,
  queue,
  pages,
  status,
  t,
  exit,
  collection,
  initialPage,
  consumePage,
}: {
  book: Book;
  queue: PersonalQueue;
  pages: Pages;
  status: SaveStatus;
  t: Translate;
  exit: () => void;
  collection?: CollectionNavigation;
  initialPage?: number;
  consumePage?: () => void;
}) {
  const title = useBookTitle();
  const [initial] = useState(readDevice);
  const count = book.content.page_count;
  // An explicit page link opens that page. Otherwise a book read to its end opens at the
  // first page again (it stays finished); any other book resumes where it was left.
  const [page, setPage] = useState(() => {
    if (initialPage !== undefined) return startingPage(count, initialPage);
    return readingStatus(book.personal) === "finished"
      ? 0
      : startingPage(count, book.personal.resume_page);
  });
  const consumed = useRef(false);
  useEffect(() => {
    if (!consumed.current) {
      consumed.current = true;
      consumePage?.();
    }
  }, [consumePage]);
  const [fit, setFit] = useState<ReadingFit>(initial.fit);
  const [direction, setDirection] = useState<ReadingDirection>(
    initial.direction,
  );
  const [seconds, setSeconds] = useState<number>(5);
  const [mode, setMode] = useState<ReadingMode>(initial.reading_mode);
  const [coverSolo, setCoverSolo] = useState(initial.cover_solo);
  const [target, setTarget] = useState({ page, serial: 0 });
  const [view, setView] = useState<ViewState | null>(null);
  const [playing, setPlaying] = useState(false);
  const [settings, setSettings] = useState(false);
  const settingsButton = useRef<HTMLButtonElement>(null);
  const settingsWasOpen = useRef(false);
  // Closing the settings panel (Escape, the close area, a choice) returns focus to its
  // button when focus was in the panel, which hides it, or nowhere.
  useEffect(() => {
    if (settingsWasOpen.current && !settings) {
      const active = document.activeElement as HTMLElement | null;
      if (
        !active ||
        active === document.body ||
        active.closest?.(".reader-settings,.settings-dismiss")
      )
        settingsButton.current?.focus({ preventScroll: true });
    }
    settingsWasOpen.current = settings;
  }, [settings]);
  const [toolsVisible, setToolsVisible] = useState(true);
  const [startRequested, setStartRequested] = useState(false);
  const [remaining, setRemaining] = useState(seconds);
  const { fullscreenNote, setFullscreenNote, fullscreenActive, fullscreen } =
    useFullscreen();
  const [flipping, setFlipping] = useState(false);
  // While the scrubber is dragged only thumbnails show; the page loads on release.
  const [scrubbing, setScrubbing] = useState(false);
  const rapid = flipping || scrubbing;
  const turnTime = useRef(-Infinity);
  const settle = useRef<ReturnType<typeof setTimeout> | undefined>(undefined);
  useEffect(() => () => clearTimeout(settle.current), []);
  const scroll = useRef<ScrollHandle>(null);
  const stageNode = useRef<HTMLElement>(null);
  // Pinch zoom of the pages only; the toolbar and scrubber keep their size.
  const [zoom, setZoom] = useState(1);
  const zoomFocus = useRef<ZoomFocus | null>(null);
  usePinchZoom(stageNode, zoom, (value, focus) => {
    zoomFocus.current = focus;
    setZoom(value);
  });
  const navigation = useMemo(
    () => Array.from({ length: count }, (_, index) => index),
    [count],
  );
  const last = count - 1;
  // Spreads are paired deterministically from the recorded page sizes.
  const ratios = useMemo(() => pageRatios(book.page_sizes), [book.page_sizes]);
  const groups = useMemo(
    () => spreads(navigation, widePages(ratios), coverSolo),
    [navigation, ratios, coverSolo],
  );
  const displayed =
    mode === "spread"
      ? (groups.find((group) => group.includes(page)) ?? [page])
      : [page];
  const next = adjacent(navigation, displayed.at(-1)!, 1);
  const before = adjacent(navigation, displayed[0]!, -1);
  const previous =
    mode === "spread" && before !== undefined
      ? groups.find((group) => group.includes(before))?.[0]
      : before;
  const frameOf = (index: number | undefined) =>
    index === undefined
      ? []
      : mode === "spread"
        ? (groups.find((group) => group.includes(index)) ?? [index])
        : [index];
  const upcoming = [...frameOf(next), ...frameOf(previous)];
  const stage =
    mode === "scroll"
      ? `${book.id}:scroll:${count}`
      : `${book.id}:${mode}:${displayed.join(",")}`;
  const activeStage = useRef(stage);
  activeStage.current = stage;
  const current = view?.stage === stage ? view : null;
  const ready = !!current?.ready;
  const error = !!current?.failed;
  const atEnd = mode === "scroll" ? !!current?.atEnd : next === undefined;
  const advanceRef = useRef(() => {});
  advanceRef.current = () => {
    if (mode === "scroll") scroll.current?.advance();
    else if (next !== undefined) setPage(next);
  };
  const playback = useMemo(
    () => new Autoplay(() => advanceRef.current(), setPlaying),
    [],
  );
  const visible = useReaderLifecycle(playback, queue);
  const progress = useWarmPages(pages, count, page, visible && !rapid);
  useLayoutEffect(() => {
    const controller = new AbortController();
    // The memory window follows the page at once, also while flipping rapidly.
    pages.focus(page);
    // Reserve the current page before the scroll window queues its neighbours. While
    // flipping rapidly only a page already downloaded loads (from the HTTP cache).
    if (!rapid || (!scrubbing && pages.downloaded(page)))
      void pages.get(page, controller.signal).catch(() => {});
    return () => controller.abort();
  }, [pages, page, rapid, scrubbing]);
  const report = (value: ViewState) => {
    if (value.stage !== activeStage.current) return;
    setView(value);
    if (mode === "scroll") setPage(value.page);
  };

  useEffect(() => {
    playback.update({
      key: current?.position ?? stage,
      ready,
      atEnd,
      visible,
      blocked: !!error || settings || status === "error",
    });
  }, [
    playback,
    current?.position,
    stage,
    ready,
    atEnd,
    visible,
    error,
    settings,
    status,
  ]);
  useEffect(() => {
    if (startRequested && !settings) {
      playback.start();
      setStartRequested(false);
      setToolsVisible(false);
    }
  }, [startRequested, settings, playback]);
  useEffect(() => {
    setRemaining(seconds);
    if (!playing || !ready) return;
    const started = Date.now();
    const timer = setInterval(
      () =>
        setRemaining(
          Math.max(0, Math.ceil(seconds - (Date.now() - started) / 1000)),
        ),
      200,
    );
    return () => clearInterval(timer);
  }, [playing, ready, current?.position, stage, seconds]);
  useEffect(() => {
    playback.setSeconds(seconds);
  }, [playback, seconds]);
  const slowSave = useReadingProgress(queue, current, visible, status, last);

  function move(destination: number | undefined) {
    playback.pause();
    if (destination === undefined) return;
    const now = performance.now();
    setFlipping(now - turnTime.current < RAPID_TURN_MS);
    turnTime.current = now;
    clearTimeout(settle.current);
    settle.current = setTimeout(() => setFlipping(false), RAPID_TURN_MS);
    setPage(destination);
    setTarget((old) => ({ page: destination, serial: old.serial + 1 }));
  }
  function togglePlayback() {
    if (playing) playback.pause();
    else if (!settings) {
      playback.start();
      if (playback.playing) setToolsVisible(false);
    }
  }
  // The left and right sides of the screen; their meaning follows the reading direction.
  const back = direction === "rtl" ? next : previous;
  const forward = direction === "rtl" ? previous : next;
  const handleReaderKey = (key: ReaderKey, event: KeyboardEvent) => {
    if (key === "escape") {
      playback.pause();
      setSettings(false);
      return;
    }
    if (key === "toggle-play") {
      event.preventDefault();
      togglePlayback();
      return;
    }
    if (key === "left" || key === "right") {
      event.preventDefault();
      if (event.repeat && performance.now() - turnTime.current < 90) return;
      // A vertical scroll has no reading direction: ← goes back and → forward.
      if (mode === "scroll") move(key === "left" ? previous : next);
      else move(key === "left" ? back : forward);
      return;
    }
    // Scroll mode leaves the remaining keys to the scrolling container.
    if (mode === "scroll") return;
    event.preventDefault();
    // A page taller than the screen (fit to width, or zoomed in) scrolls first; the
    // page turns only from its bottom (or top) edge, and only on a fresh press, so a
    // held key stops at the edge instead of running on through the book.
    if (key === "next" || key === "previous") {
      const canvas =
        stageNode.current?.querySelector<HTMLElement>(".page-canvas");
      const way = key === "next" ? 1 : -1;
      if (canvas && scrollWithin(canvas, way)) return;
      if (event.repeat && canvas && canvas.scrollHeight > canvas.clientHeight)
        return;
    }
    if (event.repeat && performance.now() - turnTime.current < 90) return;
    if (key === "next") move(next);
    else if (key === "previous") move(previous);
    else if (key === "first") move(page === 0 ? undefined : 0);
    else if (key === "last") move(displayed.includes(last) ? undefined : last);
  };
  useReaderKeys(handleReaderKey);
  // A zoomed page pans with the finger; a swipe that starts at the page's edge
  // (nothing left to pan that way) turns the page.
  const edges = useRef({ left: true, right: true });
  const measureEdges = () => {
    const canvas =
      stageNode.current?.querySelector<HTMLElement>(".page-canvas");
    if (!canvas) return;
    const end = canvas.scrollWidth - canvas.clientWidth;
    edges.current = {
      left: canvas.scrollLeft <= 2,
      right: end <= 2 || canvas.scrollLeft >= end - 2,
    };
  };
  const gestures = usePageGestures((side) => {
    if (mode === "scroll") return;
    if (zoom > 1 && !(side < 0 ? edges.current.right : edges.current.left))
      return;
    move(side < 0 ? forward : back);
  });
  // Each paged turn (and a change of mode) starts at the fitted size again.
  useEffect(() => {
    if (mode !== "scroll") setZoom(1);
  }, [mode, stage]);
  const hiddenTools = !toolsVisible
    ? { inert: true, "aria-hidden": true as const }
    : {};

  return (
    <ContextActions
      actions={[
        {
          label: t("上一页", "Previous page"),
          action: () => move(previous),
          disabled: previous === undefined,
        },
        {
          label: t("下一页", "Next page"),
          action: () => move(next),
          disabled: next === undefined,
        },
        {
          label: t("阅读设置", "Reading settings"),
          action: () => {
            playback.pause();
            setSettings(true);
          },
        },
        { label: t("书籍详情", "Book details"), action: exit },
      ]}
    >
      <main
        className={`reader ${toolsVisible ? "tools-visible" : "tools-hidden"}`}
      >
        <div
          className="reading-progress"
          role="progressbar"
          aria-label={t("阅读进度", "Reading progress")}
          aria-valuemin={0}
          aria-valuemax={count}
          aria-valuenow={(displayed.at(-1) ?? page) + 1}
          dir={direction}
        >
          {progress.done < count && (
            <span
              className="preload-progress"
              style={{
                insetInlineStart: `${(page / count) * 100}%`,
                width: `${(pages.bufferedAhead(page, count) / count) * 100}%`,
              }}
            />
          )}
          <span
            className="current-progress"
            style={{
              width: `${(((displayed.at(-1) ?? page) + 1) / count) * 100}%`,
            }}
          />
        </div>
        <header className="reader-head glass" {...hiddenTools}>
          <button
            type="button"
            className="icon-button"
            onClick={() => {
              playback.pause();
              exit();
            }}
            aria-label={t("返回详情", "Back to details")}
          >
            <ArrowLeft size={20} />
          </button>
          <div className="reader-name">
            <strong>{title(book)}</strong>
            <span>
              {status === "saving" && slowSave && (
                <Pill>{t("正在保存", "Saving")}</Pill>
              )}
              {status === "error" && <Pill>{t("尚未保存", "Not saved")}</Pill>}
            </span>
          </div>
          <button
            type="button"
            className="icon-button"
            aria-label={
              fullscreenActive
                ? t("退出全屏", "Exit fullscreen")
                : t("全屏", "Fullscreen")
            }
            aria-pressed={fullscreenActive}
            onClick={() => void fullscreen()}
          >
            {fullscreenActive ? <Minimize size={19} /> : <Maximize size={19} />}
          </button>
          <button
            ref={settingsButton}
            type="button"
            className="icon-button"
            aria-expanded={settings}
            aria-label={t("阅读设置", "Reading settings")}
            onClick={() => {
              playback.pause();
              setSettings((value) => !value);
            }}
          >
            <Settings2 size={20} />
          </button>
        </header>
        <div className="reader-notices">
          {collection && toolsVisible && (
            <nav
              className="collection-reader glass"
              aria-label={t("合集续读", "Collection reading")}
            >
              <button
                type="button"
                className="text-button"
                onClick={() => {
                  playback.pause();
                  collection.back();
                }}
              >
                {collection.name}
                {collection.index !== undefined && (
                  <Pill>
                    {collection.index + 1}/{collection.count}
                  </Pill>
                )}
              </button>
              <div className="actions">
                <button
                  type="button"
                  disabled={
                    collection.busy ||
                    !collection.previous ||
                    status === "error"
                  }
                  onClick={() => {
                    playback.pause();
                    collection.move(-1);
                  }}
                >
                  {t("上一本", "Previous book")}
                </button>
                <button
                  type="button"
                  disabled={
                    collection.busy || !collection.next || status === "error"
                  }
                  onClick={() => {
                    playback.pause();
                    collection.move(1);
                  }}
                >
                  {t("下一本", "Next book")}
                </button>
              </div>
              {collection.error && (
                <p className="small error-text" role="alert">
                  {collection.error}
                </p>
              )}
            </nav>
          )}
          {status === "error" && (
            <SaveFailure
              className="reader-warning"
              queue={queue}
              t={t}
              lead={t(
                "进度或个人修改未保存，自动播放已暂停。",
                "Changes are not saved. Autoplay is paused.",
              )}
            />
          )}
        </div>
        <ReaderSettings
          open={settings}
          close={() => setSettings(false)}
          mode={mode}
          coverSolo={coverSolo}
          fit={fit}
          direction={direction}
          t={t}
          retryPreload={
            progress.failed > 0 || progress.paused ? progress.retry : undefined
          }
          change={(value) => {
            if ("mode" in value) {
              playback.pause();
              setMode(value.mode);
              remember("reading-mode", value.mode);
              setTarget((old) => ({ page, serial: old.serial + 1 }));
            } else if ("coverSolo" in value) {
              playback.pause();
              setCoverSolo(value.coverSolo);
              remember("cover-solo", String(value.coverSolo));
            } else if ("fit" in value) {
              setFit(value.fit);
              remember("fit", value.fit);
            } else {
              setDirection(value.direction);
              remember("direction", value.direction);
            }
          }}
        />
        <section
          ref={stageNode}
          className={`page-stage mode-${mode} ${zoom > 1 ? "zoomed" : ""}`}
          aria-label={t("阅读页面", "Reading page")}
          {...gestures.handlers}
          onTouchStartCapture={measureEdges}
          onPointerDownCapture={measureEdges}
          onClickCapture={(event) => {
            if (event.detail !== 0 && gestures.moved()) {
              event.preventDefault();
              event.stopPropagation();
              return;
            }
            if ((event.target as HTMLElement).closest("button")) return;
            const bounds = event.currentTarget.getBoundingClientRect();
            const position = (event.clientX - bounds.left) / bounds.width;
            if (mode !== "scroll" && position < 0.4) move(back);
            else if (mode !== "scroll" && position > 0.6) move(forward);
            else setToolsVisible((value) => !value);
          }}
        >
          {mode !== "scroll" && (
            <button
              type="button"
              className="page-edge left"
              data-hint=""
              disabled={back === undefined}
              aria-label={
                direction === "ltr"
                  ? t("上一页", "Previous page")
                  : t("下一页", "Next page")
              }
              onClick={() => move(back)}
            >
              <ChevronLeft />
            </button>
          )}
          {mode === "scroll" ? (
            <ScrollView
              key={stage}
              ref={scroll}
              stage={stage}
              indexes={navigation}
              target={target}
              last={last}
              ratios={ratios}
              fit={fit}
              zoom={zoom}
              zoomFocus={zoomFocus}
              pages={pages}
              t={t}
              report={report}
              pause={() => playback.pause()}
            />
          ) : (
            <PagedView
              rapid={rapid}
              scrubbing={scrubbing}
              stage={stage}
              indexes={displayed}
              upcoming={upcoming}
              page={page}
              next={next}
              last={last}
              direction={direction}
              fit={fit}
              zoom={zoom}
              zoomFocus={zoomFocus}
              pages={pages}
              version={book.content.sha256}
              thumbnailRevision={book.thumbnail_revision}
              t={t}
              report={report}
              pause={() => playback.pause()}
            />
          )}
          {mode !== "scroll" && (
            <button
              type="button"
              className="page-edge right"
              data-hint=""
              disabled={forward === undefined}
              aria-label={
                direction === "ltr"
                  ? t("下一页", "Next page")
                  : t("上一页", "Previous page")
              }
              onClick={() => move(forward)}
            >
              <ChevronRight />
            </button>
          )}
        </section>
        <ReaderScrubber
          hidden={settings}
          toolsVisible={toolsVisible}
          reveal={() => setToolsVisible(true)}
          page={page}
          displayed={mode === "scroll" ? [page] : displayed}
          count={count}
          move={move}
          direction={direction}
          arrow={(key, event) =>
            handleReaderKey(
              key === "ArrowLeft"
                ? "left"
                : key === "ArrowRight"
                  ? "right"
                  : key === "ArrowDown"
                    ? "next"
                    : "previous",
              event,
            )
          }
          scrub={(active) => {
            setScrubbing(active);
            // Releasing the slider loads the chosen page at once.
            if (!active) {
              clearTimeout(settle.current);
              setFlipping(false);
            }
          }}
          playing={playing}
          seconds={seconds}
          autoplayDisabled={!!error || status === "error" || (atEnd && ready)}
          play={(value) => {
            if (value === null) playback.pause();
            else {
              setSeconds(value);
              setSettings(false);
              setStartRequested(true);
            }
          }}
          t={t}
        />
        {playing && (
          <button
            type="button"
            className="autoplay-clock glass"
            aria-label={t("暂停自动播放", "Pause autoplay")}
            onClick={() => playback.pause()}
          >
            <Timer size={16} />
            <span>{ready ? `${remaining}s` : "…"}</span>
            <Pause size={13} />
          </button>
        )}
        {fullscreenNote && (
          <Modal
            title={t("全屏阅读", "Fullscreen reading")}
            closeLabel={t("关闭", "Close")}
            close={() => setFullscreenNote(false)}
          >
            <p>
              {t(
                "在 iPhone 上，使用 Safari 的分享菜单，选择“添加到主屏幕”并以 Web App 打开，即可隐藏地址栏和标签栏。",
                "On iPhone, use Safari’s Share menu, choose Add to Home Screen and open as a Web App to hide the address and tab bars.",
              )}
            </p>
          </Modal>
        )}
      </main>
    </ContextActions>
  );
}
