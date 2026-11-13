import { InfoTip } from "./InfoTip";
import { Fragment, useState } from "react";
import { RefreshCw, Trash2 } from "lucide-react";
import { post, request, restampAllImages } from "./api";
import { bytes } from "./format";
import { ErrorNotice } from "./feedback";
import { usePolling } from "./polling";
import type { Translate } from "./types";

export type WarmStatus = {
  state: "active" | "queued" | "idle";
  done: number;
  total: number;
  rendered: number;
  failed: number;
  full?: boolean;
  finished_at: number | null;
};
type WarmKind = "covers" | "thumbnails";
export type CacheStatus = {
  cache: { budget_bytes: number; used_bytes: number };
} & Record<WarmKind, WarmStatus>;

/** One-line summary for the settings section title: what is rendering, else disk use. */
export function cacheSummary(status: CacheStatus, t: Translate) {
  const { covers, thumbnails, cache } = status;
  if (covers.state === "active")
    return t(
      `正在生成封面 ${covers.done}/${covers.total}`,
      `Rendering covers ${covers.done}/${covers.total}`,
    );
  if (thumbnails.state === "active")
    return t(
      `正在生成缩略图 ${thumbnails.done}/${thumbnails.total}`,
      `Rendering thumbnails ${thumbnails.done}/${thumbnails.total}`,
    );
  return t(
    `已用 ${bytes(cache.used_bytes)} / ${bytes(cache.budget_bytes)}`,
    `${bytes(cache.used_bytes)} of ${bytes(cache.budget_bytes)} used`,
  );
}

function warmText(kind: WarmKind, warm: WarmStatus, t: Translate) {
  if (warm.state === "active")
    return t(
      `进行中 ${warm.done}/${warm.total} 本`,
      `Running ${warm.done}/${warm.total} books`,
    );
  if (warm.state === "queued")
    return kind === "thumbnails"
      ? t("排队中，封面生成完后开始", "Queued; starts after covers")
      : t("排队中", "Queued");
  if (!warm.finished_at) return t("等待开始", "Waiting to start");
  if (warm.full)
    return t(
      `缓存将满，已在 ${warm.done}/${warm.total} 本处停止（新生成 ${warm.rendered} 张）`,
      `Stopped at ${warm.done}/${warm.total} books, cache nearly full (${warm.rendered} rendered)`,
    );
  // Both warm-ups count failures per book (a book has one cover).
  return t(
    `已完成 ${warm.total} 本：新生成 ${warm.rendered} 张，失败 ${warm.failed} 本`,
    `Finished ${warm.total} books: ${warm.rendered} rendered, ${warm.failed} books failed`,
  );
}

/** Cover and page thumbnail cache: disk use against its budget and the two background warm-ups. */
export function CacheSettings({ t }: { t: Translate }) {
  const [data, setData] = useState<CacheStatus | null>(null),
    [busy, setBusy] = useState<WarmKind | null>(null),
    [actionError, setActionError] = useState<unknown>(null);
  const running =
    !!data &&
    (["covers", "thumbnails"] as const).some(
      (kind) => data[kind].state !== "idle",
    );
  const { error, refresh } = usePolling(
    async (signal) => {
      const value = await request<CacheStatus>("/api/cache", { signal });
      if (!signal.aborted) setData(value);
    },
    running ? 2000 : 15_000,
  );
  const warm = async (kind: WarmKind) => {
    setBusy(kind);
    setActionError(null);
    try {
      setData(await post<CacheStatus>(`/api/cache/${kind}/warm`));
      await refresh();
    } catch (failure) {
      setActionError(failure);
    } finally {
      setBusy(null);
    }
  };
  const labels = {
    covers: {
      name: t("封面", "Covers"),
      progress: t("封面生成进度", "Cover progress"),
      action: t("补全缺失的封面", "Render missing covers"),
    },
    thumbnails: {
      name: t("缩略图", "Thumbnails"),
      progress: t("缩略图生成进度", "Thumbnail progress"),
      action: t("补全缺失的缩略图", "Render missing thumbnails"),
    },
  };
  return (
    <section className="cache-settings">
      <ErrorNotice error={error || actionError} t={t} />
      {data && (
        <>
          <dl className="settings-facts">
            {(["covers", "thumbnails"] as const).map((kind) => (
              <Fragment key={kind}>
                <dt>{labels[kind].name}</dt>
                <dd className="cache-warm">
                  {warmText(kind, data[kind], t)}
                  {data[kind].state === "active" && (
                    <progress
                      className="cache-progress"
                      value={data[kind].done}
                      max={Math.max(1, data[kind].total)}
                      aria-label={labels[kind].progress}
                    />
                  )}
                </dd>
              </Fragment>
            ))}
          </dl>
          <div className="cache-actions">
            {(["covers", "thumbnails"] as const).map((kind) => (
              <button
                key={kind}
                type="button"
                disabled={busy !== null || data[kind].state !== "idle"}
                onClick={() => warm(kind)}
              >
                <RefreshCw size={16} />
                {labels[kind].action}
              </button>
            ))}
          </div>
        </>
      )}
      <DeviceCache t={t} />
    </section>
  );
}

/**
 * This browser's own cache of pages, covers and thumbnails. The server asks the browser
 * to empty it (Clear-Site-Data, honoured on HTTPS and localhost); on plain HTTP the
 * images get new addresses instead, so the old entries go unused and the browser drops
 * them over time. Sign-in and settings stay either way.
 */
function DeviceCache({ t }: { t: Translate }) {
  const [state, setState] = useState<"idle" | "busy" | "done">("idle");
  const [error, setError] = useState<unknown>(null);
  return (
    <>
      <h4 className="heading-with-info">
        {t("本机图片缓存", "Images cached on this device")}
        <InfoTip
          label={t("关于本机图片缓存", "About images cached on this device")}
        >
          {t(
            "清空后图片会重新下载；登录和设置不受影响。",
            "Images download again after clearing; sign-in and settings stay.",
          )}
        </InfoTip>
      </h4>
      <ErrorNotice error={error} t={t} />
      <div className="cache-actions">
        <button
          type="button"
          disabled={state === "busy"}
          onClick={async () => {
            setState("busy");
            setError(null);
            try {
              await post("/api/client-cache/clear");
              restampAllImages();
              setState("done");
            } catch (failure) {
              setError(failure);
              setState("idle");
            }
          }}
        >
          <Trash2 size={16} />
          {t("清空本机缓存", "Clear this device's cache")}
        </button>
        {state === "done" && (
          <span role="status" className="muted small">
            {window.isSecureContext
              ? t("已清空。", "Cleared.")
              : t(
                  "已停用旧缓存；通过 HTTP 访问时浏览器会逐步自行清除旧数据。",
                  "Old entries are no longer used; over plain HTTP the browser removes them over time.",
                )}
          </span>
        )}
      </div>
    </>
  );
}
