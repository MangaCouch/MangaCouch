import {
  forwardRef,
  useEffect,
  useImperativeHandle,
  useLayoutEffect,
  useMemo,
  useRef,
  useState,
} from "react";
import { PageImage } from "./PageImage";
import type { ImageState, ViewState } from "./PageImage";
import type { Pages } from "./pages";
import { anchoredTop, scrollHeight, scrollRows, scrollWindow } from "./reading";
import type { ScrollRow } from "./reading";
import type { ReadingFit } from "./device";
import type { Translate } from "./types";

export interface ScrollHandle {
  advance: () => void;
}
export const ScrollView = forwardRef<
  ScrollHandle,
  {
    stage: string;
    indexes: number[];
    target: { page: number; serial: number };
    last: number | undefined;
    /** Width/height per page, from the book's recorded page sizes. */
    ratios: ReadonlyMap<number, number>;
    fit?: ReadingFit;
    pages: Pages;
    t: Translate;
    report: (view: ViewState) => void;
    pause: () => void;
  }
>(function ScrollView(
  {
    stage,
    indexes,
    target,
    last,
    ratios,
    fit = "width",
    pages,
    t,
    report,
    pause,
  },
  ref,
) {
  const root = useRef<HTMLDivElement>(null);
  useEffect(() => {
    root.current?.focus({ preventScroll: true });
  }, []);
  const [viewport, setViewport] = useState({ top: 0, width: 800, height: 600 });
  const [states, setStates] = useState<Record<number, ImageState>>({});
  // Counts autoplay advances so a step within one tall page is still a new position.
  const [advances, setAdvances] = useState(0);
  const live = useRef(true),
    callbacks = useRef({ report, pause });
  callbacks.current = { report, pause };
  const width = viewport.width;
  const rows = useMemo(
    () =>
      scrollRows(
        indexes,
        width,
        ratios,
        Math.max(120, viewport.height / 4),
        fit === "height" ? viewport.height : undefined,
      ),
    [indexes, width, ratios, viewport.height, fit],
  );
  const total = scrollHeight(rows),
    maximum = Math.max(0, total - viewport.height);
  const top = Math.min(viewport.top, maximum);
  const range = scrollWindow(rows, top, viewport.height);
  const mounted = rows.slice(range.start, range.end);
  const current = rows[range.active];
  const visible = rows
    .slice(range.first, range.last + 1)
    .filter(
      (row) => row.top < top + viewport.height && row.top + row.height > top,
    );
  const ready =
    visible.length > 0 && visible.every((row) => states[row.page]?.ready);
  const failed = visible.some((row) => states[row.page]?.failed);
  const atEnd = top >= maximum - 2;
  const lastRow = rows.find((row) => row.page === last);
  const completed =
    ready &&
    !!lastRow &&
    lastRow.top + lastRow.height <= top + viewport.height + 2 &&
    lastRow.top + lastRow.height > top;
  const previous = useRef<ScrollRow[]>([]);
  const previousTarget = useRef<typeof target | null>(null);
  const latest = useRef({
    top,
    maximum,
    height: viewport.height,
    scrollTop: viewport.top,
  });
  latest.current = {
    top,
    maximum,
    height: viewport.height,
    scrollTop: viewport.top,
  };

  useLayoutEffect(() => {
    const node = root.current;
    if (!node) return;
    const measure = () =>
      setViewport((old) => ({
        ...old,
        width: Math.max(1, node.clientWidth),
        height: Math.max(1, node.clientHeight),
      }));
    measure();
    const observer = new ResizeObserver(measure);
    observer.observe(node);
    return () => observer.disconnect();
  }, []);
  useLayoutEffect(() => {
    const scrollTop = latest.current.scrollTop;
    const jumped = previousTarget.current !== target;
    const desired = jumped
      ? (rows.find((row) => row.page === target.page)?.top ?? 0)
      : anchoredTop(previous.current, rows, scrollTop);
    const value = Math.max(0, Math.min(maximum, desired));
    previous.current = rows;
    previousTarget.current = target;
    if (root.current && Math.abs(root.current.scrollTop - value) > 0.5)
      root.current.scrollTop = value;
    if (Math.abs(scrollTop - value) > 0.5)
      setViewport((old) => ({ ...old, top: value }));
  }, [rows, target, maximum]);
  useImperativeHandle(
    ref,
    () => ({
      advance() {
        const node = root.current;
        if (!node) return;
        const state = latest.current;
        const value = Math.min(
          state.maximum,
          state.top + Math.max(1, state.height * 0.85),
        );
        node.scrollTop = value;
        setViewport((old) => ({ ...old, top: value }));
        setAdvances((count) => count + 1);
      },
    }),
    [],
  );
  useEffect(() => {
    live.current = true;
    return () => {
      live.current = false;
    };
  }, []);
  // The parent only hears about discrete changes (page, readiness, end, completion,
  // an autoplay step); the pixel position stays local so scrolling does not
  // re-render the whole reader.
  const page = current?.page;
  const activeReady = page !== undefined && !!states[page]?.ready;
  const position = `${stage}:${advances}:${visible.map((row) => row.page).join(",")}`;
  useEffect(() => {
    if (page === undefined) return;
    callbacks.current.report({
      stage,
      position,
      page,
      ready,
      activeReady,
      failed,
      atEnd,
      completed,
    });
  }, [stage, position, page, ready, activeReady, failed, atEnd, completed]);
  return (
    <div
      ref={root}
      className="page-canvas scroll-reader"
      tabIndex={0}
      role="region"
      aria-label={t("纵向滚动阅读", "Vertical scrolling reader")}
      onScroll={(event) => {
        const node = event.currentTarget;
        setViewport((old) => ({ ...old, top: Math.max(0, node.scrollTop) }));
      }}
      onWheel={() => callbacks.current.pause()}
      onTouchStart={() => callbacks.current.pause()}
      onPointerDown={() => callbacks.current.pause()}
      onKeyDown={(event) => {
        if (
          [
            "ArrowUp",
            "ArrowDown",
            "PageUp",
            "PageDown",
            "Home",
            "End",
          ].includes(event.key)
        )
          callbacks.current.pause();
      }}
    >
      <div className="scroll-flow" style={{ width, height: total }}>
        {mounted.map((row) => (
          <div
            key={row.page}
            className={`scroll-slot ${fit === "height" ? "scroll-fit-height" : ""}`}
            data-page={row.page}
            style={{ top: row.top, height: row.height }}
          >
            <PageImage
              pages={pages}
              page={row.page}
              t={t}
              onState={(page, state) => {
                if (live.current)
                  setStates((old) =>
                    old[page]?.ready === state.ready &&
                    old[page]?.failed === state.failed
                      ? old
                      : { ...old, [page]: state },
                  );
              }}
              onRetry={() => callbacks.current.pause()}
            />
            <span className="scroll-page-label">{row.page + 1}</span>
          </div>
        ))}
      </div>
    </div>
  );
});
