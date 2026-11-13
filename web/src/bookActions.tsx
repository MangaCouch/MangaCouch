import { useEffect, useRef, useState } from "react";
import type { ReactNode } from "react";
import { LoaderCircle } from "lucide-react";
import { Modal } from "./Modal";
import { Select } from "./Controls";
import type { MenuAction, MenuEntry } from "./Controls";
import { RemovalDialog } from "./RemovalDialog";
import { aborted, ApiError, errorMessage, post, request } from "./api";
import { pollDelay } from "./polling";
import { copyText } from "./clipboard";
import { toast } from "./feedback";
import { jobId } from "./identifiers";
import { searchOptions } from "./LibraryFilters";
import type { Browse } from "./LibraryFilters";
import type { Book, Collection, Translate } from "./types";

/** What a book menu acts on: chosen books, or every search result but a few. */
export type BookTargets = {
  /** The chosen books (all of them unless `all` is set). */
  bookIds: string[];
  all?: { browse: Browse; total: number; exclude: string[] };
  /** The book the menu was opened on; "read" and "edit" use it. */
  anchor: Book;
  /** Whether any target is unfinished; decides between "mark read" and "mark unread". */
  unfinished: boolean | null;
};
type Action =
  | { kind: "completed"; completed: boolean }
  | { kind: "rating"; rating: number | null }
  | { kind: "source" }
  | { kind: "add_collection"; target_id: string; target_revision: number };
type Job = {
  id: string;
  state: "running" | "done" | "cancelled";
  counts: Record<string, number>;
};

/** How often a running bulk job is polled; the server advances it in the background. */
export const BULK_POLL_MS = 500;
/** Polls of one job before the menu stops following it (half an hour of visible time). */
const MAX_POLLS = 3600;
/** The thumbnail refresh endpoint takes at most this many books per request. */
const CHUNK = 500;
/** Collections per page the picker requests (the server's largest page). */
const COLLECTION_PAGE = 200;
/** Opening a book's editor from elsewhere: the detail page checks this once. */
export const EDIT_INTENT = "mangacouch:edit-metadata";

const targetCount = (targets: BookTargets) =>
  targets.all
    ? targets.all.total - targets.all.exclude.length
    : targets.bookIds.length;
/** A job outlived MAX_POLLS: it keeps running on the server, the menu stops waiting. */
class StillRunning extends Error {}
const chunks = <T,>(items: T[]) =>
  Array.from({ length: Math.ceil(items.length / CHUNK) }, (_, i) =>
    items.slice(i * CHUNK, (i + 1) * CHUNK),
  );

/** The ids of the targets; selecting all asks the server for the current result. */
async function targetIds(targets: BookTargets) {
  if (!targets.all) return targets.bookIds;
  const { browse, total, exclude } = targets.all;
  const result = await post<{ items: string[] }>(
    "/api/removals/search/selection",
    {
      query: browse.query,
      options: searchOptions(browse),
      expected_count: total,
      exclude,
    },
  );
  return result.items;
}

/** Submits the complete selection once; the server persists and batches the job. */
async function runJobs(
  targets: BookTargets,
  action: Action,
  progress: (done: number, total: number, job: string) => void,
  signal: AbortSignal,
) {
  const total = targetCount(targets);
  const id = jobId();
  let job: Job;
  if (targets.all) {
    const { browse, total: expected, exclude } = targets.all;
    job = await post<Job>("/api/bulk-search", {
      id,
      action,
      query: browse.query,
      options: searchOptions(browse),
      expected_count: expected,
      exclude,
    });
  } else {
    job = await post<Job>("/api/bulk-selection", {
      id,
      action,
      book_ids: targets.bookIds,
    });
  }
  for (let polls = 0; job.state === "running"; polls++) {
    if (polls === MAX_POLLS) throw new StillRunning();
    progress(total - (job.counts.pending ?? total), total, job.id);
    await pollDelay(BULK_POLL_MS, signal);
    for (let retry = 0; ; retry++) {
      try {
        job = await request<Job>(`/api/bulk/${job.id}?limit=1`, { signal });
        break;
      } catch (error) {
        if (aborted(error)) throw error;
        if (error instanceof ApiError && error.status !== 503) throw error;
        if (retry >= 5) throw new StillRunning();
        await pollDelay(Math.min(16000, 1000 * 2 ** retry), signal);
      }
    }
  }
  return job.counts;
}

/**
 * The book menu shared by single books and selections, with the dialogs it opens:
 * read, copy link | mark read/unread, add to collection, rating | edit metadata,
 * refresh thumbnails, review metadata | remove.
 */
export function useBookActions({
  t,
  navigate,
  changed,
  removed,
}: {
  t: Translate;
  navigate: (route: string) => void;
  /** Books changed: reload what is shown. */
  changed: () => void;
  /** These books were moved to the recycle bin. */
  removed?: (ids: string[]) => void;
}) {
  const [collecting, setCollecting] = useState<BookTargets | null>(null);
  const [removing, setRemoving] = useState<string[] | null>(null);
  // Jobs keep running on the server; leaving the page only stops following them.
  const lifetime = useRef<AbortController | null>(null);
  useEffect(() => {
    const controller = new AbortController();
    lifetime.current = controller;
    return () => controller.abort();
  }, []);

  async function run(targets: BookTargets, action: Action, done: ReactNode) {
    const signal = lifetime.current?.signal ?? AbortSignal.abort();
    const id = toast.loading(t("正在处理…", "Working…"));
    try {
      const counts = await runJobs(
        targets,
        action,
        (finished, total, job) => {
          toast.loading(
            t(
              `正在处理 ${finished} / ${total}`,
              `Working ${finished} / ${total}`,
            ),
            {
              id,
              action: {
                label: t("停止未完成项", "Stop remaining"),
                onClick: () => {
                  void post(`/api/bulk/${job}/cancel`).catch((error) =>
                    toast.error(errorMessage(error, t)),
                  );
                },
              },
            },
          );
        },
        signal,
      );
      changed();
      const failed =
        (counts.conflict ?? 0) + (counts.error ?? 0) + (counts.missing ?? 0);
      if (counts.cancelled)
        toast(
          t(
            `已停止，${counts.done ?? 0} 本已完成，${counts.cancelled} 本未执行。`,
            `Stopped: ${counts.done ?? 0} completed, ${counts.cancelled} not applied.`,
          ),
          { id, duration: 8000 },
        );
      else if (failed)
        toast.error(
          t(
            `${failed} 本未处理（已变化、无来源或已移除）。`,
            `${failed} not done (changed, no source, or removed).`,
          ),
          { id, duration: 8000 },
        );
      else if (counts.ignored)
        toast(
          t(
            `已处理 ${counts.done ?? 0} 本；自动忽略 ${counts.ignored} 本已退役来源。`,
            `Processed ${counts.done ?? 0}; ignored ${counts.ignored} retired sources.`,
          ),
          { id },
        );
      else toast.success(done, { id });
    } catch (error) {
      if (
        targets.all &&
        error instanceof ApiError &&
        error.code === "revision_conflict"
      ) {
        changed();
        toast.error(
          t(
            "搜索结果已变化，已刷新，请重试",
            "The results changed and were refreshed; try again",
          ),
          { id },
        );
        return;
      }
      if (aborted(error) || error instanceof StillRunning) {
        changed();
        toast(
          t(
            "任务仍在服务器上运行，稍后刷新即可看到结果。",
            "The job continues on the server; refresh later to see the result.",
          ),
          { id, duration: 8000 },
        );
        return;
      }
      toast.error(errorMessage(error, t), { id, duration: 8000 });
    }
  }
  async function withIds(
    targets: BookTargets,
    act: (ids: string[]) => Promise<void> | void,
  ) {
    try {
      await act(await targetIds(targets));
    } catch (error) {
      if (
        targets.all &&
        error instanceof ApiError &&
        error.code === "revision_conflict"
      ) {
        changed();
        toast.error(
          t(
            "搜索结果已变化，已刷新，请重试",
            "The results changed and were refreshed; try again",
          ),
        );
        return;
      }
      toast.error(errorMessage(error, t), { duration: 8000 });
    }
  }
  const copyLinks = (targets: BookTargets) =>
    withIds(targets, async (ids) => {
      const base = `${location.origin}${location.pathname}`;
      if (await copyText(ids.map((id) => `${base}#book/${id}`).join("\n")))
        toast.success(
          ids.length === 1
            ? t("已复制链接", "Link copied")
            : t(`已复制 ${ids.length} 个链接`, `${ids.length} links copied`),
        );
      else
        toast.error(
          t(
            "复制失败：浏览器不允许此页面使用剪贴板。",
            "Copy failed: the browser blocked clipboard access for this page.",
          ),
          { duration: 8000 },
        );
    });

  /** Renders the targets' cover and page thumbnails again, then reloads what is shown. */
  function refreshThumbnails(targets: BookTargets) {
    void withIds(targets, async (ids) => {
      try {
        for (const part of chunks(ids))
          await post("/api/thumbnails/refresh", { book_ids: part });
      } finally {
        changed();
      }
      toast.success(t("缩略图将重新生成", "Thumbnails will be rendered again"));
    });
  }

  function menu(
    targets: BookTargets,
    overrides: {
      read?: () => void;
      edit?: () => void;
      remove?: () => void;
      review?: () => void;
      completed?: (value: boolean) => void;
      rating?: (value: number | null) => void;
      disabled?: boolean;
    } = {},
  ): MenuEntry[] {
    const single = !targets.all && targets.bookIds.length === 1;
    const rating = (value: number | null): MenuAction => ({
      label:
        value === null
          ? t("清除评分", "Clear rating")
          : t(`${value} 分`, `${value} ★`),
      action: () =>
        overrides.rating
          ? overrides.rating(value)
          : void run(
              targets,
              { kind: "rating", rating: value },
              t("评分已更新", "Rating updated"),
            ),
    });
    return [
      {
        label: t("阅读", "Read"),
        action: overrides.read ?? (() => navigate(`read/${targets.anchor.id}`)),
      },
      {
        label: single
          ? t("复制链接", "Copy link")
          : t("复制链接", "Copy links"),
        action: () => void copyLinks(targets),
      },
      { separator: true },
      targets.unfinished === null
        ? {
            label: t("正在加载阅读状态…", "Loading reading status…"),
            disabled: true,
            action: () => {},
          }
        : targets.unfinished
          ? {
              label: t("标为已读", "Mark as read"),
              action: () =>
                overrides.completed
                  ? overrides.completed(true)
                  : void run(
                      targets,
                      { kind: "completed", completed: true },
                      t("已标为已读", "Marked as read"),
                    ),
            }
          : {
              label: t("标为未读", "Mark as unread"),
              action: () =>
                overrides.completed
                  ? overrides.completed(false)
                  : void run(
                      targets,
                      { kind: "completed", completed: false },
                      t("已标为未读", "Marked as unread"),
                    ),
            },
      {
        label: t("加入合集…", "Add to collection…"),
        action: () => setCollecting(targets),
      },
      {
        label: t("评分", "Rating"),
        items: [
          ...[5, 4.5, 4, 3.5, 3, 2.5, 2, 1.5, 1, 0.5].map(rating),
          rating(null),
        ],
      },
      { separator: true },
      ...(single
        ? [
            {
              label: t("编辑元数据", "Edit metadata"),
              action: () => {
                if (overrides.edit) {
                  overrides.edit();
                  return;
                }
                try {
                  sessionStorage.setItem(EDIT_INTENT, targets.anchor.id);
                } catch {
                  /* Without storage the detail page simply opens. */
                }
                navigate(`book/${targets.anchor.id}`);
              },
            },
          ]
        : []),
      {
        label: t("刷新缩略图", "Refresh thumbnails"),
        action: () => refreshThumbnails(targets),
      },
      {
        label: overrides.review
          ? t("核对元数据", "Review metadata")
          : t("在线检查元数据", "Check metadata online"),
        action:
          overrides.review ??
          (() =>
            void run(
              targets,
              { kind: "source" },
              <>
                {t(
                  "已加入核对队列，在后台依次进行；失败或需核对的会列在",
                  "Queued for review; it runs in the background. Failed checks and books awaiting review are listed in ",
                )}
                <a href="#settings/source-checks">
                  {t("设置 → 来源核对", "Settings → Source review")}
                </a>
                {t(
                  "，也会显示在书籍详情页。",
                  " and shown on the book's page.",
                )}
              </>,
            )),
      },
      { separator: true },
      {
        label: single
          ? t("移除…", "Remove…")
          : t(
              `移除 ${targetCount(targets)} 本…`,
              `Remove ${targetCount(targets)}…`,
            ),
        danger: true,
        disabled: overrides.disabled,
        action:
          overrides.remove ??
          (() => void withIds(targets, (ids) => setRemoving(ids))),
      },
    ];
  }

  const dialogs = (
    <>
      {collecting && (
        <CollectionPicker
          t={t}
          count={targetCount(collecting)}
          close={() => setCollecting(null)}
          choose={(collection) => {
            const targets = collecting;
            setCollecting(null);
            void run(
              targets,
              {
                kind: "add_collection",
                target_id: collection.id,
                target_revision: collection.revision,
              },
              t(
                `已加入「${collection.name}」`,
                `Added to “${collection.name}”`,
              ),
            );
          }}
        />
      )}
      {removing && (
        <RemovalDialog
          bookIds={removing}
          t={t}
          close={() => setRemoving(null)}
          removed={(ids) => {
            setRemoving(null);
            removed?.(ids);
            changed();
          }}
        />
      )}
    </>
  );
  return {
    menu,
    dialogs,
    /** Opens the collection picker for the targets. */
    collect: setCollecting,
    copyLinks,
    refreshThumbnails,
  };
}

/** Chooses a static collection to add books to. */
function CollectionPicker({
  t,
  count,
  close,
  choose,
}: {
  t: Translate;
  count: number;
  close: () => void;
  choose: (collection: Collection) => void;
}) {
  const [collections, setCollections] = useState<Collection[] | null>(null);
  const [target, setTarget] = useState("");
  const [error, setError] = useState<unknown>(null);
  useEffect(() => {
    const controller = new AbortController();
    // Every page: "no static collections" is only true once the last page is seen.
    async function load() {
      const found: Collection[] = [];
      let after = "";
      do {
        const page = await request<{
          items: Collection[];
          after: string | null;
        }>(
          `/api/collections?kind=static&counts=false&limit=${COLLECTION_PAGE}&after=${encodeURIComponent(after)}`,
          { signal: controller.signal },
        );
        found.push(...page.items.filter((item) => item.kind !== "dynamic"));
        after = page.after ?? "";
      } while (after);
      return found;
    }
    void load()
      .then((found) => {
        if (!controller.signal.aborted) setCollections(found);
      })
      .catch((error) => {
        if (!controller.signal.aborted) setError(error);
      });
    return () => controller.abort();
  }, []);
  const chosen = collections?.find((item) => item.id === target);
  return (
    <Modal
      title={t(`加入合集（${count} 本）`, `Add to collection (${count})`)}
      closeLabel={t("关闭", "Close")}
      close={close}
    >
      {!!error && (
        <p role="alert" className="notice error">
          {errorMessage(error, t)}
        </p>
      )}
      {collections === null && !error ? (
        <p role="status">
          <LoaderCircle size={16} className="spinner" />{" "}
          {t("正在载入…", "Loading…")}
        </p>
      ) : collections?.length === 0 ? (
        <p className="muted">
          {t(
            "还没有静态合集，请先在合集页新建。",
            "No static collections yet; create one on the Collections page.",
          )}
        </p>
      ) : (
        <Select
          aria-label={t("合集", "Collection")}
          value={target}
          onChange={(event) => setTarget(event.target.value)}
        >
          <option value="">
            {t("选择静态合集", "Choose a static collection")}
          </option>
          {collections?.map((item) => (
            <option key={item.id} value={item.id}>
              {item.name}
            </option>
          ))}
        </Select>
      )}
      <div className="actions modal-actions">
        <button type="button" onClick={close}>
          {t("取消", "Cancel")}
        </button>
        <button
          type="button"
          className="primary"
          disabled={!chosen}
          onClick={() => chosen && choose(chosen)}
        >
          {t("加入", "Add")}
        </button>
      </div>
    </Modal>
  );
}
