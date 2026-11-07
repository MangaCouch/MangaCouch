import { useEffect, useRef, useState } from "react";
import { LoaderCircle } from "lucide-react";
import { pageError, ImageLoadError, useObjectUrl } from "./PageImage";
import type { ViewState } from "./PageImage";
import { thumbnailPath } from "./pages";
import type { Pages } from "./pages";
import type { ReadingDirection, ReadingFit } from "./device";
import type { Translate } from "./types";

type Frame = {
  stage: string;
  images: { index: number; url: string }[];
};
/** A cached page usually decodes within this time; only slower pages show a thumbnail. */
export const PREVIEW_DELAY_MS = 120;

export function PagedView({
  stage,
  indexes,
  page,
  next,
  last,
  direction,
  fit,
  pages,
  version,
  t,
  report,
  pause,
  select,
  rapid = false,
}: {
  stage: string;
  indexes: number[];
  page: number;
  next: number | undefined;
  last: number | undefined;
  direction: ReadingDirection;
  fit: ReadingFit;
  pages: Pages;
  /** Content hash used to version thumbnail URLs. */
  version?: string;
  t: Translate;
  report: (view: ViewState) => void;
  pause: () => void;
  select: (page: number) => void;
  rapid?: boolean;
}) {
  // A spread shows at most two pages; each slot loads through the shared cache.
  const first = useObjectUrl(pages, indexes[0], !rapid);
  const second = useObjectUrl(pages, indexes[1], !rapid);
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
  const ready = frame?.stage === stage;
  const error = slots.find((slot) => slot.error)?.error ?? null;
  const urls = slots.map((slot) => slot.url);
  const loaded = urls.every(Boolean) ? urls.join(" ") : "";
  // Thumbnails stand in only for uncached pages: at once while flipping rapidly
  // (full pages are skipped then), otherwise only when a page is slow to arrive.
  const [previewStage, setPreviewStage] = useState("");
  const uncached = indexes.some((index) => !pages.has(index));
  useEffect(() => {
    if (ready || !uncached) return;
    const timer = setTimeout(() => setPreviewStage(stage), PREVIEW_DELAY_MS);
    return () => clearTimeout(timer);
  }, [stage, ready, uncached]);
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
  useEffect(() => {
    if (rapid || !loaded || shown.current?.stage === stage) return;
    const controller = new AbortController();
    const decoding = loaded.split(" ").map(async (url, slot) => {
      const image = new Image();
      image.src = url;
      try {
        await image.decode();
      } catch (error) {
        if (!controller.signal.aborted)
          failures.current[slot]!(new ImageLoadError());
        throw error;
      }
      return { index: [firstIndex, secondIndex][slot]!, url };
    });
    // A failed decode is already reported through its slot.
    Promise.all(decoding).then(
      (images) => {
        if (controller.signal.aborted) return;
        shown.current = { stage, images };
        setFrame(shown.current);
      },
      () => {},
    );
    return () => controller.abort();
  }, [stage, rapid, loaded, firstIndex, secondIndex]);
  return (
    <div
      className={`page-canvas paged-canvas fit-${fit} ${indexes.length === 2 ? "spread-canvas" : "single-canvas"} ${ready ? "frame-ready" : "frame-loading"}`}
      aria-busy={!ready}
    >
      {frame && (
        <div
          className={`page-spread spread-ready retained-frame ${direction === "rtl" ? "rtl" : ""}`}
        >
          {frame.images.map((image) => (
            <div
              className={`page-image image-ready ${image.index === page ? "selected-page" : ""}`}
              data-page={image.index}
              key={image.index}
            >
              <img
                src={image.url}
                alt={`${t("第", "Page")} ${image.index + 1}`}
                draggable={false}
              />
              {ready && indexes.length > 1 && (
                <button
                  type="button"
                  className="page-number"
                  aria-pressed={image.index === page}
                  aria-label={t(
                    `选中第 ${image.index + 1} 页`,
                    `Select page ${image.index + 1}`,
                  )}
                  onClick={(event) => {
                    event.stopPropagation();
                    select(image.index);
                  }}
                >
                  {image.index + 1}
                </button>
              )}
            </div>
          ))}
        </div>
      )}
      {!ready && uncached && (rapid || previewStage === stage) && (
        <div
          className={`page-spread reader-preview ${direction === "rtl" ? "rtl" : ""}`}
          aria-hidden="true"
          key={stage}
        >
          {indexes.map((index) => (
            <div className="page-image" key={index}>
              <img
                src={thumbnailPath(pages.bookId, index, version)}
                alt=""
                decoding="async"
                onLoad={(event) => {
                  event.currentTarget.style.opacity = "1";
                }}
              />
            </div>
          ))}
        </div>
      )}
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
