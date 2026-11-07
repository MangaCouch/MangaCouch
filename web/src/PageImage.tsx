import { useEffect, useRef, useState } from "react";
import { aborted, errorMessage } from "./api";
import type { Pages } from "./pages";
import type { Translate } from "./types";

export interface ImageState {
  ready: boolean;
  failed: boolean;
}
export interface ViewState {
  stage: string;
  position: string;
  page: number;
  ready: boolean;
  activeReady: boolean;
  failed: boolean;
  atEnd: boolean;
  completed: boolean;
}

/**
 * Loads one page through the shared cache and exposes it as an object URL. The URL is
 * revoked when it is replaced or the component unmounts; `retry()` fetches again and
 * `fail()` records an error found later (for example while decoding).
 */
export function useObjectUrl(
  pages: Pages,
  index: number | undefined,
  enabled = true,
) {
  const [attempt, setAttempt] = useState(0);
  const [state, setState] = useState<{
    key: string;
    url?: string;
    error?: unknown;
  } | null>(null);
  const key = `${index}:${attempt}`;
  // The key whose load already finished, so re-enabling does not fetch it again.
  const settled = useRef<{ pages: Pages; key: string } | null>(null);
  useEffect(() => {
    if (
      index === undefined ||
      !enabled ||
      (settled.current?.pages === pages && settled.current.key === key)
    )
      return;
    const controller = new AbortController();
    pages.get(index, controller.signal).then(
      (blob) => {
        if (controller.signal.aborted) return;
        settled.current = { pages, key };
        setState({ key, url: URL.createObjectURL(blob) });
      },
      (error: unknown) => {
        if (controller.signal.aborted || aborted(error)) return;
        settled.current = { pages, key };
        setState({ key, error });
      },
    );
    return () => controller.abort();
  }, [pages, index, enabled, key]);
  useEffect(() => {
    const url = state?.url;
    return () => {
      if (url) URL.revokeObjectURL(url);
    };
  }, [state]);
  const current = state?.key === key ? state : null;
  return {
    key,
    url: current?.url ?? null,
    error: current?.error ?? null,
    retry: () => setAttempt((value) => value + 1),
    fail: (error: unknown) => {
      if (index !== undefined) pages.invalidate(index);
      setState({ key, error });
    },
  };
}

export class ImageLoadError extends Error {
  constructor() {
    super("The page image could not be displayed");
  }
}

/** Message for a page that failed to load or display. */
export const pageError = (error: unknown, t: Translate) =>
  error instanceof ImageLoadError
    ? t("页面图片无法显示。", "The page image could not be shown.")
    : errorMessage(error, t);

export function PageImage({
  pages,
  page,
  t,
  onState,
  onRetry,
}: {
  pages: Pages;
  page: number;
  t: Translate;
  onState: (page: number, state: ImageState) => void;
  onRetry: () => void;
}) {
  const image = useObjectUrl(pages, page);
  const [readyKey, setReadyKey] = useState("");
  const callbacks = useRef({ onState, onRetry });
  callbacks.current = { onState, onRetry };
  const ready = readyKey === image.key && !image.error;
  const failed = !!image.error;
  useEffect(() => {
    callbacks.current.onState(page, { ready, failed });
  }, [page, ready, failed]);
  useEffect(
    () => () =>
      callbacks.current.onState(page, { ready: false, failed: false }),
    [page],
  );
  return (
    <div
      className={`page-image ${ready ? "image-ready" : "image-pending"}`}
      data-page={page}
    >
      {!ready && !failed && (
        <div className="page-loading" role="status">
          {t("正在加载页面…", "Loading page…")}
        </div>
      )}
      {failed ? (
        <div className="page-error" role="alert">
          <p>{pageError(image.error, t)}</p>
          <button
            type="button"
            onClick={(event) => {
              event.stopPropagation();
              callbacks.current.onRetry();
              image.retry();
            }}
          >
            {t("重新加载", "Retry page")}
          </button>
        </div>
      ) : (
        image.url && (
          <img
            src={image.url}
            alt={`${t("第", "Page")} ${page + 1}`}
            onLoad={() => setReadyKey(image.key)}
            onError={() => {
              setReadyKey("");
              image.fail(new ImageLoadError());
            }}
            draggable={false}
          />
        )
      )}
    </div>
  );
}
