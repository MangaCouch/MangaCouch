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
import { useBookTitle, usePreferences } from "./PreferenceContext";
import { readDevice, remember } from "./device";
import type { ReadingDirection, ReadingFit } from "./device";
import { ReaderSettings } from "./ReaderSettings";
import { ReaderScrubber } from "./ReaderScrubber";
import { usePageGestures, useReaderKeys } from "./readerInput";
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
}: {
  book: Book;
  queue: PersonalQueue;
  pages: Pages;
  status: SaveStatus;
  t: Translate;
  exit: () => void;
  collection?: CollectionNavigation;
  initialPage?: number;
}) {
  const title = useBookTitle();
  const { device } = usePreferences();
  const [initial] = useState(() => readDevice(device));
  const count = book.content.page_count;
  const [page, setPage] = useState(
    startingPage(count, initialPage ?? book.personal.resume_page),
  );
  const [fit, setFit] = useState<ReadingFit>(initial.fit);
  const [direction, setDirection] = useState<ReadingDirection>(
    initial.direction,
  );
  const [seconds, setSeconds] = useState<number>(initial.autoplay_seconds);
  const [mode, setMode] = useState<ReadingMode>(initial.reading_mode);
  const [coverSolo, setCoverSolo] = useState(initial.cover_solo);
  const [target, setTarget] = useState({ page, serial: 0 });
  const [view, setView] = useState<ViewState | null>(null);
  const [playing, setPlaying] = useState(false);
  const [settings, setSettings] = useState(false);
  const [toolsVisible, setToolsVisible] = useState(true);
  const [startRequested, setStartRequested] = useState(false);
  const [remaining, setRemaining] = useState(seconds);
  const { fullscreenNote, setFullscreenNote, fullscreenActive, fullscreen } =
    useFullscreen();
  const [turn, setTurn] = useState(1);
  const [rapid, setRapid] = useState(false);
  const turnTime = useRef(-Infinity);
  const settle = useRef<ReturnType<typeof setTimeout> | undefined>(undefined);
  useEffect(() => () => clearTimeout(settle.current), []);
  const scroll = useRef<ScrollHandle>(null);
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
    else if (next !== undefined) {
      setTurn(direction === "rtl" ? -1 : 1);
      setPage(next);
    }
  };
  const playback = useMemo(
    () => new Autoplay(() => advanceRef.current(), setPlaying),
    [],
  );
  const visible = useReaderLifecycle(playback, queue);
  const progress = useWarmPages(pages, count, page, true, visible && !rapid);
  useLayoutEffect(() => {
    const controller = new AbortController();
    // Reserve the current page before the scroll window queues its neighbours.
    if (!rapid) void pages.get(page, controller.signal).catch(() => {});
    return () => controller.abort();
  }, [pages, page, rapid]);
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
    remember("autoplay-seconds", String(seconds));
  }, [playback, seconds]);
  const slowSave = useReadingProgress(queue, current, visible, status);

  function move(destination: number | undefined) {
    playback.pause();
    if (destination === undefined) return;
    const now = performance.now();
    setRapid(now - turnTime.current < RAPID_TURN_MS);
    turnTime.current = now;
    clearTimeout(settle.current);
    settle.current = setTimeout(() => setRapid(false), RAPID_TURN_MS);
    setTurn((destination >= page ? 1 : -1) * (direction === "rtl" ? -1 : 1));
    setPage(destination);
    setTarget((old) => ({ page: destination, serial: old.serial + 1 }));
  }
  function togglePlayback() {
    if (playing) playback.pause();
    else if (!settings) {
      playback.start();
      setToolsVisible(false);
    }
  }
  // The left and right sides of the screen; their meaning follows the reading direction.
  const back = direction === "rtl" ? next : previous;
  const forward = direction === "rtl" ? previous : next;
  useReaderKeys((key, event) => {
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
      move(key === "left" ? back : forward);
      return;
    }
    // Scroll mode leaves the remaining keys to the scrolling container.
    if (mode === "scroll") return;
    event.preventDefault();
    if (event.repeat && performance.now() - turnTime.current < 90) return;
    if (key === "next") move(next);
    else if (key === "previous") move(previous);
    else if (key === "first") move(page === 0 ? undefined : 0);
    else if (key === "last") move(displayed.includes(last) ? undefined : last);
  });
  const gestures = usePageGestures((side) => {
    if (mode !== "scroll") move(side < 0 ? forward : back);
  });
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
        style={{ "--turn": turn } as React.CSSProperties}
      >
        <div
          className="reading-progress"
          role="progressbar"
          aria-label={t("阅读进度", "Reading progress")}
          aria-valuemin={0}
          aria-valuemax={count}
          aria-valuenow={page + 1}
        >
          {progress.done < count && (
            <span
              className="preload-progress"
              style={{
                left: `${(page / count) * 100}%`,
                width: `${(pages.bufferedAhead(page, count) / count) * 100}%`,
              }}
            />
          )}
          <span
            className="current-progress"
            style={{ width: `${((page + 1) / count) * 100}%` }}
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
                  collection.busy || !collection.previous || status === "error"
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
        {status === "error" && (
          <div className="reader-warning" role="alert">
            {t(
              "进度或个人修改未保存，自动播放已暂停。",
              "Changes are not saved. Autoplay is paused.",
            )}{" "}
            <button type="button" onClick={() => void queue.retry()}>
              {t("重试保存", "Retry saving")}
            </button>{" "}
            <button type="button" onClick={() => void queue.reload()}>
              {t(
                "丢弃未保存修改并重新载入",
                "Discard unsaved changes and reload",
              )}
            </button>
          </div>
        )}
        <section
          className={`page-stage mode-${mode}`}
          aria-label={t("阅读页面", "Reading page")}
          {...gestures.handlers}
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
              pages={pages}
              t={t}
              report={report}
              pause={() => playback.pause()}
            />
          ) : (
            <PagedView
              rapid={rapid}
              stage={stage}
              indexes={displayed}
              page={page}
              next={next}
              last={last}
              direction={direction}
              fit={fit}
              pages={pages}
              version={book.content.sha256}
              t={t}
              report={report}
              pause={() => playback.pause()}
              select={move}
            />
          )}
          {mode !== "scroll" && (
            <button
              type="button"
              className="page-edge right"
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
          count={count}
          move={move}
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
