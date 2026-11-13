import { useEffect, useLayoutEffect, useRef, useState } from "react";
import type { RefObject } from "react";
import { zoomedOffset } from "./readerInput";
import type { ZoomFocus } from "./readerInput";
import { LoaderCircle } from "lucide-react";
import { pageError, ImageLoadError, useObjectUrl } from "./PageImage";
import type { ViewState } from "./PageImage";
import { thumbnailPath } from "./pages";
import type { Pages } from "./pages";
import type { ReadingDirection, ReadingFit } from "./device";
import type { Translate } from "./types";

/** How long a downloaded page may take before it counts as coming from the network. */
const SLOW_MS = 250;

type Frame = {
  stage: string;
  images: { index: number; url: string }[];
};

/** Scrolls a canvas to where reading a page starts: its top, at the reading direction's side. */
function toStart(node: HTMLElement, rtl: boolean) {
  node.scrollTop = 0;
  node.scrollLeft = rtl ? Math.max(0, node.scrollWidth - node.clientWidth) : 0;
}

/*
 * What a turn shows (every change is an immediate swap, never an animation):
 * - a page in memory or downloaded before (the browser's HTTP cache serves it in about
 *   a millisecond): the new frame replaces the old one as soon as it is decoded, even
 *   while flipping rapidly;
 * - a page never downloaded (or one the HTTP cache lost, noticed when it is not ready
 *   within SLOW_MS): the previous frame blurs and dims until a thumbnail arrives,
 *   then the thumbnails cover it at full page size (in a spread each thumbnail shows
 *   as soon as it arrives), and the full page replaces them;
 * - while the scrubber is dragged: thumbnails replace the frame directly, with no
 *   blur, and the full page loads on release.
 */

export function PagedView({
  stage,
  indexes,
  upcoming = [],
  page,
  next,
  last,
  direction,
  fit,
  zoom = 1,
  zoomFocus,
  pages,
  version,
  thumbnailRevision,
  t,
  report,
  pause,
  rapid = false,
  scrubbing = false,
}: {
  stage: string;
  indexes: number[];
  /** Pages of the neighbouring frames, decoded ahead so a cached turn shows at once. */
  upcoming?: number[];
  page: number;
  next: number | undefined;
  last: number | undefined;
  direction: ReadingDirection;
  fit: ReadingFit;
  /** Pinch zoom of the pages; the canvas scrolls over the enlarged frame. */
  zoom?: number;
  /** The pinch midpoint of the last zoom change, kept in place on screen. */
  zoomFocus?: RefObject<ZoomFocus | null>;
  pages: Pages;
  /** Content hash used to version thumbnail URLs. */
  version?: string;
  thumbnailRevision: number;
  t: Translate;
  report: (view: ViewState) => void;
  pause: () => void;
  /** Rapid flipping or scrubbing: no full page downloads start. */
  rapid?: boolean;
  /** The scrubber is being dragged: thumbnails only, no blur. */
  scrubbing?: boolean;
}) {
  // A spread shows at most two pages; each slot loads through the shared cache. While
  // flipping rapidly only pages already downloaded load (from the HTTP cache); while
  // scrubbing none do.
  const quick = (index: number | undefined) =>
    index !== undefined && !scrubbing && pages.downloaded(index);
  const first = useObjectUrl(pages, indexes[0], !rapid || quick(indexes[0]));
  const second = useObjectUrl(pages, indexes[1], !rapid || quick(indexes[1]));
  const slots = [first, second].slice(0, indexes.length);
  const firstIndex = indexes[0],
    secondIndex = indexes[1];
  const failures = useRef([first.fail, second.fail]);
  failures.current = [first.fail, second.fail];
  const includesLast = last !== undefined && indexes.includes(last);
  const [frame, setFrame] = useState<Frame | null>(null);
  const shown = useRef<Frame | null>(null);
  const callbacks = useRef({ report });
  callbacks.current = { report };
  const canvas = useRef<HTMLDivElement>(null),
    shownZoom = useRef(zoom);
  const rtl = useRef(direction === "rtl");
  rtl.current = direction === "rtl";
  // Set when a new frame showed while still zoomed: the turn's zoom reset that follows
  // must not move the new page away from its start.
  const restart = useRef(false);
  useLayoutEffect(() => {
    const node = canvas.current,
      before = shownZoom.current;
    shownZoom.current = zoom;
    if (!node || before === zoom) return;
    if (restart.current) {
      restart.current = false;
      if (zoom === 1) {
        toStart(node, rtl.current);
        return;
      }
    }
    const focus = zoomFocus?.current,
      bounds = node.getBoundingClientRect();
    const x = focus?.x ?? bounds.left + bounds.width / 2,
      y = focus?.y ?? bounds.top + bounds.height / 2;
    node.scrollLeft = zoomedOffset(
      node.scrollLeft,
      x,
      bounds.left,
      before,
      zoom,
    );
    node.scrollTop = zoomedOffset(node.scrollTop, y, bounds.top, before, zoom);
  }, [zoom, zoomFocus]);
  // The canvas stays mounted across turns: each new frame starts at the top of the
  // page (and at the right edge for right-to-left reading), not where the last one was.
  const frameStage = frame?.stage;
  useLayoutEffect(() => {
    const node = canvas.current;
    if (!node || frameStage === undefined) return;
    toStart(node, rtl.current);
    restart.current = shownZoom.current !== 1;
  }, [frameStage]);
  const ready = frame?.stage === stage;
  // Decoded images by object URL; holding them keeps the decoded bitmaps warm.
  const decoded = useRef(new Map<string, HTMLImageElement>());
  const upcomingKey = upcoming.join(",");
  /** Keeps a decoded image, and only those of the pages shown and coming up next. */
  const keepDecoded = useRef((_url: string, _image: HTMLImageElement) => {});
  keepDecoded.current = (url, image) => {
    const keep = new Set(
      [...indexes, ...upcoming].map((index) => pages.url(index)),
    );
    keep.add(url);
    decoded.current.set(url, image);
    for (const key of decoded.current.keys())
      if (!keep.has(key)) decoded.current.delete(key);
  };
  useEffect(() => {
    if (!ready || rapid) return;
    const controller = new AbortController();
    const around = upcomingKey ? upcomingKey.split(",").map(Number) : [];
    for (const index of around) {
      pages
        .get(index, controller.signal, true)
        .then(async () => {
          const url = pages.url(index);
          if (!url || decoded.current.has(url) || controller.signal.aborted)
            return;
          const image = new Image();
          image.src = url;
          await image.decode();
          if (!controller.signal.aborted) keepDecoded.current(url, image);
        })
        .catch(() => {});
    }
    return () => controller.abort();
  }, [ready, rapid, upcomingKey, pages]);
  const error = slots.find((slot) => slot.error)?.error ?? null;
  const urls = slots.map((slot) => slot.url);
  const loaded = urls.every(Boolean) ? urls.join(" ") : "";
  // A downloaded page that is still not ready after SLOW_MS came from the network after
  // all (the HTTP cache dropped it), so it gets the thumbnail treatment too.
  // Counted per visit, so a slow load of this frame earlier does not count again.
  const visit = useRef({ stage, id: 0 });
  if (visit.current.stage !== stage)
    visit.current = { stage, id: visit.current.id + 1 };
  const visitId = visit.current.id;
  const [slow, setSlow] = useState(-1);
  useEffect(() => {
    if (ready) return;
    const timer = setTimeout(() => setSlow(visitId), SLOW_MS);
    return () => clearTimeout(timer);
  }, [ready, visitId]);
  const uncached = indexes.some(
    (index) =>
      !pages.has(index) && (!pages.downloaded(index) || slow === visitId),
  );
  // The thumbnail layer keeps its <img> slots across a drag; a turn starts a new layer.
  const previewKey = useRef({ stage, key: stage });
  if (previewKey.current.stage !== stage)
    previewKey.current = {
      stage,
      key: scrubbing ? previewKey.current.key : stage,
    };
  const layer = previewKey.current.key;
  // Set once the layer's first thumbnail arrives; it then hides the previous frame.
  const [cover, setCover] = useState<string | null>(null);
  const covered = !ready && cover === layer;
  useEffect(() => {
    if (ready) setCover(null);
  }, [ready]);
  const show = useRef<(images: Frame["images"]) => void>(() => {});
  show.current = (images) => {
    shown.current = { stage, images };
    setFrame(shown.current);
  };
  const showPreview = !ready && (uncached || scrubbing);
  const waiting = !ready && uncached && !scrubbing && !covered;
  useEffect(() => {
    callbacks.current.report({
      stage,
      position: stage,
      page,
      ready,
      activeReady: ready,
      failed: !!error,
      atEnd: next === undefined,
      completed: ready && includesLast,
    });
  }, [stage, page, ready, error, next, includesLast]);
  // A layout effect, so pages decoded ahead replace the frame before the next paint.
  // Cached pages show even while flipping rapidly; only scrubbing waits for release.
  useLayoutEffect(() => {
    if (scrubbing || !loaded || shown.current?.stage === stage) return;
    const urls = loaded.split(" ");
    const indexOf = (slot: number) => [firstIndex, secondIndex][slot]!;
    if (urls.every((url) => decoded.current.has(url))) {
      show.current(urls.map((url, slot) => ({ index: indexOf(slot), url })));
      return;
    }
    const controller = new AbortController();
    const decoding = urls.map(async (url, slot) => {
      const image = new Image();
      image.src = url;
      try {
        await image.decode();
      } catch (error) {
        if (!controller.signal.aborted)
          failures.current[slot]!(new ImageLoadError());
        throw error;
      }
      keepDecoded.current(url, image);
      return { index: indexOf(slot), url };
    });
    // A failed decode is already reported through its slot.
    Promise.all(decoding).then(
      (images) => {
        if (!controller.signal.aborted) show.current(images);
      },
      () => {},
    );
    return () => controller.abort();
  }, [stage, scrubbing, loaded, firstIndex, secondIndex]);
  return (
    <div
      ref={canvas}
      className={`page-canvas paged-canvas fit-${fit} ${indexes.length === 2 ? "spread-canvas" : "single-canvas"} ${ready ? "frame-ready" : "frame-loading"} ${waiting ? "frame-waiting" : ""}`}
      aria-busy={!ready}
    >
      <div
        className="page-zoom"
        style={
          zoom > 1
            ? { transform: `scale(${zoom})`, transformOrigin: "0 0" }
            : undefined
        }
      >
        {frame && (
          <div
            key={frame.stage}
            className={`page-spread spread-ready retained-frame ${direction === "rtl" ? "rtl" : ""}`}
          >
            {frame.images.map((image) => (
              <div
                className="page-image image-ready"
                data-page={image.index}
                key={image.index}
              >
                <img
                  src={image.url}
                  alt={t(`第 ${image.index + 1} 页`, `Page ${image.index + 1}`)}
                  draggable={false}
                />
              </div>
            ))}
          </div>
        )}
        {showPreview && (
          <div
            key={layer}
            className={`page-spread reader-preview ${direction === "rtl" ? "rtl" : ""} ${covered ? "covered" : ""}`}
            aria-hidden="true"
          >
            {/* Slots keep their <img>, so a new thumbnail cancels the previous request
                and the last thumbnail stays until the next one arrives. */}
            {indexes.map((index, slot) => (
              <div className="page-image" key={slot}>
                <img
                  src={thumbnailPath(
                    pages.bookId,
                    index,
                    version,
                    thumbnailRevision,
                  )}
                  alt=""
                  decoding="async"
                  onLoad={(event) => {
                    event.currentTarget.style.opacity = "1";
                    setCover(layer);
                  }}
                />
              </div>
            ))}
          </div>
        )}
      </div>
      {!ready && (
        <div
          className="page-transition-status"
          role={error ? "alert" : "status"}
        >
          {error ? (
            <>
              <span>{pageError(error, t)}</span>
              <button
                type="button"
                onClick={(event) => {
                  event.stopPropagation();
                  pause();
                  slots.forEach((slot) => slot.retry());
                }}
              >
                {t("重试", "Retry")}
              </button>
            </>
          ) : (
            <>
              <LoaderCircle size={16} className="spinner" />
              <span>{t(`第 ${page + 1} 页`, `Page ${page + 1}`)}</span>
            </>
          )}
        </div>
      )}
    </div>
  );
}
