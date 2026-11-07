import { jobId } from "./identifiers";
import { useEffect, useRef, useState } from "react";
import { LoaderCircle } from "lucide-react";
import { Modal } from "./Modal";
import { Select, Segmented } from "./Controls";
import { Rating } from "./Rating";
import { RemovalDialog } from "./RemovalDialog";
import { UserError, aborted, errorMessage, post, request } from "./api";
import { searchOptions } from "./LibraryFilters";
import type { LibrarySelection } from "./BookShelf";
import type { Collection, Translate } from "./types";

type Job = {
  id: string;
  state: "running" | "done";
  counts: Record<string, number>;
};
/** How often a running bulk job is polled; the server advances it in the background. */
export const BULK_POLL_MS = 500;

/** Resolves after `ms` unless `signal` aborts first. */
const wait = (ms: number, signal: AbortSignal) =>
  new Promise<void>((resolve, reject) => {
    const done = () => {
      signal.removeEventListener("abort", stop);
      resolve();
    };
    const timer = setTimeout(done, ms);
    const stop = () => {
      clearTimeout(timer);
      reject(signal.reason);
    };
    signal.addEventListener("abort", stop, { once: true });
  });

export function BulkActions({
  selection,
  t,
  close,
  changed,
}: {
  selection: LibrarySelection;
  t: Translate;
  close: () => void;
  changed: () => void;
}) {
  const [kind, setKind] = useState("completed"),
    [completed, setCompleted] = useState(true),
    [rating, setRating] = useState<number | null>(null),
    [target, setTarget] = useState("");
  const [collections, setCollections] = useState<Collection[]>([]),
    [busy, setBusy] = useState(false),
    [error, setError] = useState<unknown>(null),
    [progress, setProgress] = useState(""),
    [removing, setRemoving] = useState<string[] | null>(null);
  const identifier = useRef(jobId());
  /** Cancels the running action: polling, result loading or per-book updates. */
  const running = useRef<AbortController | null>(null);
  useEffect(() => () => running.current?.abort(), []);
  const count = selection.all
    ? selection.all.total - selection.all.exclude.length
    : selection.books.length;
  useEffect(() => {
    const c = new AbortController();
    void request<{ collections: Collection[] }>("/api/collections?limit=200", {
      signal: c.signal,
    })
      .then((data) =>
        setCollections(
          data.collections.filter((item) => item.kind !== "dynamic"),
        ),
      )
      .catch((error) => {
        if (!c.signal.aborted) setError(error);
      });
    return () => c.abort();
  }, []);
  async function resolveIds(signal: AbortSignal) {
    if (!selection.all) return selection.books.map((book) => book.id);
    const result = await post<{ book_ids: string[] }>(
      "/api/removals/search/selection",
      {
        query: selection.all.browse.query,
        options: searchOptions(selection.all.browse),
        expected_count: selection.all.total,
        exclude: selection.all.exclude,
      },
      signal,
    );
    return result.book_ids;
  }
  /** Creates a server-side job and polls it until the server reports it done. */
  async function runJob(signal: AbortSignal) {
    const action =
      kind === "completed"
        ? { kind, completed }
        : kind === "source"
          ? { kind }
          : kind === "rating"
            ? { kind, rating }
            : {
                kind: "add_collection",
                target_id: target,
                target_revision: collections.find((item) => item.id === target)
                  ?.revision,
              };
    let job = await post<Job>(
      selection.all ? "/api/bulk-search" : "/api/bulk",
      selection.all
        ? {
            id: identifier.current,
            action,
            query: selection.all.browse.query,
            options: searchOptions(selection.all.browse),
            expected_count: selection.all.total,
            exclude: selection.all.exclude,
          }
        : {
            id: identifier.current,
            action,
            targets: selection.books.map((book) => ({
              book_id: book.id,
              metadata_revision: book.metadata_revision,
              personal_revision: book.personal_revision,
            })),
          },
      signal,
    );
    while (job.state !== "done") {
      setProgress(`${count - (job.counts.pending ?? 0)} / ${count}`);
      await wait(BULK_POLL_MS, signal);
      job = await request<Job>(`/api/bulk/${job.id}?limit=1`, { signal });
    }
    return job;
  }
  async function apply() {
    if (busy) return;
    const controller = new AbortController();
    running.current = controller;
    setBusy(true);
    setError(null);
    setProgress("");
    try {
      if (kind === "remove") {
        setRemoving(await resolveIds(controller.signal));
        return;
      }
      const job = await runJob(controller.signal);
      changed();
      identifier.current = jobId();
      if (
        (job.counts.conflict ?? 0) +
          (job.counts.error ?? 0) +
          (job.counts.missing ?? 0) >
        0
      )
        throw new UserError(
          t(
            "部分书籍已变化或处理失败，请重新选择后重试。",
            "Some books changed or failed. Select them again to retry.",
          ),
        );
      setProgress(
        kind === "source"
          ? t(
              "已加入更新队列，可在设置中的来源核对列表查看进度。",
              "Queued for update. Follow progress in Settings → Source checks.",
            )
          : t("已完成", "Complete"),
      );
    } catch (error) {
      identifier.current = jobId();
      if (aborted(error) || controller.signal.aborted)
        setProgress(
          kind === "remove"
            ? t("已停止。", "Stopped.")
            : t(
                "已停止查看；服务器会继续完成此操作。",
                "Stopped watching; the server still finishes this action.",
              ),
        );
      else setError(error);
    } finally {
      if (running.current === controller) running.current = null;
      setBusy(false);
    }
  }
  if (removing)
    return (
      <RemovalDialog
        bookIds={removing}
        t={t}
        close={() => setRemoving(null)}
        removed={() => {
          changed();
          close();
        }}
      />
    );
  return (
    <Modal
      title={t(`批量操作（${count} 本）`, `Bulk actions (${count} books)`)}
      closeLabel={t("关闭", "Close")}
      close={() => {
        // Closing stops watching or loading; a server-side job keeps running.
        running.current?.abort();
        close();
      }}
      dismissOutside={false}
    >
      <label>
        {t("操作", "Action")}
        <Select
          value={kind}
          disabled={busy}
          onChange={(event) => {
            identifier.current = jobId();
            setKind(event.target.value);
            setProgress("");
          }}
        >
          <option value="completed">{t("阅读状态", "Reading status")}</option>
          <option value="add_collection">
            {t("加入合集", "Add to collection")}
          </option>
          <option value="rating">{t("评分", "Rating")}</option>
          <option value="source">
            {t("从来源更新", "Update from source")}
          </option>
          <option value="remove">{t("移除", "Remove")}</option>
        </Select>
      </label>
      {kind === "completed" && (
        <Segmented
          label={t("阅读状态", "Reading status")}
          value={String(completed)}
          change={(v) => setCompleted(v === "true")}
          options={[
            { value: "true", label: t("已读", "Read") },
            { value: "false", label: t("未读", "Unread") },
          ]}
        />
      )}
      {kind === "rating" && <Rating value={rating} change={setRating} t={t} />}
      {kind === "add_collection" && (
        <Select
          aria-label={t("合集", "Collection")}
          value={target}
          onChange={(event) => setTarget(event.target.value)}
        >
          <option value="">
            {t("选择静态合集", "Choose a static collection")}
          </option>
          {collections.map((item) => (
            <option key={item.id} value={item.id}>
              {item.name}
            </option>
          ))}
        </Select>
      )}
      {!!error && (
        <p role="alert" className="notice error">
          {errorMessage(error, t)}
        </p>
      )}
      <p role="status">{progress}</p>
      <div className="actions">
        <button
          type="button"
          className={kind === "remove" ? "danger" : "primary"}
          disabled={busy || !count || (kind === "add_collection" && !target)}
          onClick={() => void apply()}
        >
          {busy && <LoaderCircle size={16} className="spinner" />}
          {kind === "remove" ? t("继续", "Continue") : t("应用", "Apply")}
        </button>
        {busy ? (
          <button type="button" onClick={() => running.current?.abort()}>
            {t("停止", "Stop")}
          </button>
        ) : (
          <button type="button" onClick={close}>
            {t("关闭", "Close")}
          </button>
        )}
      </div>
    </Modal>
  );
}
