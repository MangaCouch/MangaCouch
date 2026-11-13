import { useEffect, useMemo, useRef, useState } from "react";
import { LoaderCircle } from "lucide-react";
import { Modal } from "./Modal";
import { useAction } from "./action";
import { aborted, ApiError, errorMessage, post, request } from "./api";
import { toast } from "./feedback";
import { books as bookCount, dateTime } from "./format";
import type { Book, Translate } from "./types";
import { useBookTitle } from "./PreferenceContext";
type Preview = {
  id: string;
  book_id: string;
  token: string;
  title: string;
  blocked: string[];
  delete_blocked?: string[];
};
export function RemovalDialog({
  books,
  bookIds,
  t,
  close,
  removed,
}: {
  books?: Book[];
  bookIds?: string[];
  t: Translate;
  close: () => void;
  removed: (ids: string[]) => void;
}) {
  const title = useBookTitle();
  const ids = useMemo(
    () => bookIds ?? books?.map((book) => book.id) ?? [],
    [bookIds, books],
  );
  const [batches, setBatches] = useState<
    (Preview & { book_ids: string[]; items: Preview[] })[]
  >([]);
  const [previews, setPreviews] = useState<Preview[]>([]),
    [loaded, setLoaded] = useState(false),
    [deleteFile, setDeleteFile] = useState(false),
    [done, setDone] = useState<string[]>([]);
  const [refresh, setRefresh] = useState(0);
  const finished = useRef(new Set<string>());
  const stopped = useRef(false);
  const filesKept = useRef(false);
  const action = useAction();
  const { busy, error, setError } = action;
  useEffect(() => {
    const controller = new AbortController();
    setLoaded(false);
    void (async () => {
      const remaining = ids.filter((id) => !finished.current.has(id));
      if (ids.length > 1) {
        const groups: (Preview & { book_ids: string[]; items: Preview[] })[] =
          [];
        for (let offset = 0; offset < remaining.length; offset += 1000) {
          groups.push(
            ...(await Promise.all(
              [
                remaining.slice(offset, offset + 500),
                remaining.slice(offset + 500, offset + 1000),
              ]
                .filter((group) => group.length)
                .map((group) =>
                  request<Preview & { book_ids: string[]; items: Preview[] }>(
                    "/api/removals/selected/preview",
                    {
                      method: "POST",
                      body: JSON.stringify({
                        book_ids: group,
                      }),
                      signal: controller.signal,
                    },
                  ),
                ),
            )),
          );
        }
        if (!controller.signal.aborted) {
          setBatches(groups);
          setPreviews(groups.flatMap((group) => group.items));
          setLoaded(true);
          setError(null);
        }
        return;
      }
      const identifier = remaining[0];
      if (!identifier) return;
      const preview = await request<Preview>("/api/removals/preview", {
        method: "POST",
        body: JSON.stringify({ book_id: identifier }),
        signal: controller.signal,
      });
      if (!controller.signal.aborted) {
        setPreviews([preview]);
        setLoaded(true);
        setError(null);
      }
    })().catch((error) => {
      if (!aborted(error)) setError(error);
    });
    return () => controller.abort();
  }, [ids, setError, refresh]);
  const recheck =
    previews.some(
      (item) =>
        item.blocked.length > 0 || (deleteFile && item.delete_blocked?.length),
    ) ||
    (error instanceof ApiError && error.status === 409);
  const finish = (completed: string[]) => {
    (filesKept.current ? toast.warning : toast.success)(
      filesKept.current
        ? t(
            "记录已移除，但部分文件未删除；请在设置的记录回收站查看原因。",
            "Records removed, but some files were kept. Check the recycle bin in Settings.",
          )
        : t(
            `已移除 ${completed.length} 本书`,
            `Removed ${bookCount(completed.length)}`,
          ),
    );
    removed(completed);
  };
  async function remove() {
    const completed = [...done];
    stopped.current = false;
    const record = (result: {
      deletion_stopped?: string | null;
      delete_pending?: boolean;
      items?: { file: string }[];
    }) => {
      if (
        deleteFile &&
        (result.deletion_stopped ||
          result.delete_pending ||
          result.items?.some((item) => item.file !== "deleted"))
      )
        filesKept.current = true;
    };
    await action.run(async () => {
      if (batches.length) {
        for (const batch of batches) {
          if (stopped.current) break;
          if (batch.book_ids.every((id) => completed.includes(id))) continue;
          record(
            await post("/api/removals/selected", {
              id: batch.id,
              book_ids: batch.book_ids,
              token: batch.token,
              confirmed: true,
              delete_file: deleteFile,
            }),
          );
          completed.push(...batch.book_ids);
          batch.book_ids.forEach((id) => finished.current.add(id));
          setDone([...completed]);
        }
        return;
      }
      const [preview] = previews;
      if (preview && !completed.includes(preview.book_id)) {
        record(
          await post("/api/removals", {
            id: preview.id,
            book_id: preview.book_id,
            token: preview.token,
            confirmed: true,
            delete_file: deleteFile,
          }),
        );
        completed.push(preview.book_id);
        finished.current.add(preview.book_id);
        setDone([...completed]);
      }
    });
    if (completed.length === ids.length || stopped.current) finish(completed);
  }
  // One book is named: by the book given, or (from a library menu) by its preview.
  const single =
    ids.length === 1
      ? books?.[0]
        ? title(books[0])
        : previews[0]?.title || null
      : null;
  return (
    <Modal
      title={
        single
          ? t(`将移除《${single}》`, `Remove “${single}”`)
          : t(`将移除 ${ids.length} 本书`, `Remove ${bookCount(ids.length)}`)
      }
      closeLabel={t("取消", "Cancel")}
      close={() => {
        if (done.length) finish(done);
        else close();
      }}
      busy={busy}
      dismissOutside={false}
    >
      {ids.length > 1 && (
        <ul className="removal-titles">
          {previews.slice(0, 100).map((item) => (
            <li key={item.book_id}>{item.title || item.book_id}</li>
          ))}
          {previews.length > 100 && (
            <li>
              {t(
                `另有 ${previews.length - 100} 本`,
                `${previews.length - 100} more books`,
              )}
            </li>
          )}
        </ul>
      )}
      <label className="checkbox">
        <input
          type="checkbox"
          checked={deleteFile}
          disabled={busy}
          onChange={(event) => setDeleteFile(event.target.checked)}
        />
        {t("同时移除文件", "Also delete files")}
      </label>
      {deleteFile && (
        <p className="notice error" role="status">
          {t(
            "文件将从磁盘永久删除，无法恢复",
            "Files will be permanently deleted from disk and cannot be recovered.",
          )}
        </p>
      )}
      {!!error && (
        <p className="notice error" role="alert">
          {errorMessage(error, t)}
        </p>
      )}
      {previews.some(
        (item) =>
          item.blocked.length > 0 ||
          (deleteFile && item.delete_blocked?.length),
      ) && (
        <p className="notice">
          {[
            ...new Set(
              previews.flatMap((item) => [
                ...item.blocked,
                ...(deleteFile ? (item.delete_blocked ?? []) : []),
              ]),
            ),
          ]
            .map((code) =>
              errorMessage(
                new ApiError(
                  409,
                  `removal_${code === "import_recovery_pending" ? "import_pending" : code}`,
                ),
                t,
              ),
            )
            .join(" ")}
        </p>
      )}
      {!loaded && !error && (
        <p role="status">{t("正在检查文件…", "Checking files…")}</p>
      )}
      {recheck && (
        <button
          disabled={busy}
          onClick={() => {
            setError(null);
            setRefresh((value) => value + 1);
          }}
        >
          {t("重新检查", "Check again")}
        </button>
      )}
      <div className="actions modal-actions">
        {busy && batches.length > 1 && (
          <button
            onClick={() => {
              stopped.current = true;
            }}
          >
            {t("停止后续批次", "Stop remaining batches")}
          </button>
        )}
        <button
          disabled={busy}
          onClick={() => {
            if (done.length) finish(done);
            else close();
          }}
        >
          {t("取消", "Cancel")}
        </button>
        <button
          className="danger"
          disabled={
            busy ||
            !loaded ||
            previews.some(
              (item) =>
                item.blocked.length > 0 ||
                (deleteFile && item.delete_blocked?.length),
            )
          }
          onClick={() => void remove()}
        >
          {busy && <LoaderCircle size={16} className="spinner" />}
          {deleteFile
            ? t("移除并删除文件", "Remove and delete files")
            : t("确认移除", "Remove")}
        </button>
      </div>
      {!deleteFile && (
        <p className="muted small">
          {t(
            "可在 设置 → 记录回收站 中 7 天内恢复",
            "Restore within 7 days in Settings → Record recycle bin.",
          )}
        </p>
      )}
    </Modal>
  );
}
/** A removal receipt as listed by GET /api/removals. */
type Receipt = {
  id: string;
  kind: "single" | "selected";
  title: string | null;
  book_ids: string[];
  created_at: number;
  delete_requested?: boolean;
  delete_pending: boolean;
  deletion_stopped?: string | null;
  restored: boolean;
  can_restore: boolean;
  items?: {
    book_id: string;
    title: string | null;
    filename: string;
    file: "kept" | "deleted";
    restored: boolean;
  }[];
};
const RECYCLE_PAGE = 50;
export function RecycleBin({ t }: { t: Translate }) {
  const [items, setItems] = useState<Receipt[]>([]),
    [after, setAfter] = useState<string | null>(null),
    [loading, setLoading] = useState(true),
    [busy, setBusy] = useState<string | null>(null);
  const [loadError, setLoadError] = useState<unknown>(null);
  const action = useAction();
  const error = action.error ?? loadError;
  /** Loads the newest page, or the older page `cursor` continues to and appends it. */
  async function load(cursor: string | null = null, signal?: AbortSignal) {
    const result = await request<{ items: Receipt[]; after: string | null }>(
      `/api/removals?limit=${RECYCLE_PAGE}${cursor === null ? "" : `&after=${encodeURIComponent(cursor)}`}`,
      { signal },
    );
    if (signal?.aborted) return;
    setItems((old) =>
      cursor === null ? result.items : [...old, ...result.items],
    );
    setAfter(result.after);
    setLoadError(null);
  }
  useEffect(() => {
    const controller = new AbortController();
    void load(null, controller.signal)
      .catch((error) => {
        if (!aborted(error)) setLoadError(error);
      })
      .finally(() => setLoading(false));
    return () => controller.abort();
  }, []);
  async function act(id: string, task: () => Promise<unknown>) {
    setBusy(id);
    await action.run(async () => {
      await task();
      await load();
    });
    setBusy(null);
  }
  const fileState = (item: NonNullable<Receipt["items"]>[number]) =>
    item.restored
      ? t("已恢复", "Restored")
      : item.file === "deleted"
        ? t("文件已删除", "File deleted")
        : t("文件保留", "File kept");
  return (
    <section>
      {!!error && (
        <p className="notice error" role="alert">
          {errorMessage(error, t)}
        </p>
      )}
      {!loading && !items.length && (
        <p className="muted">{t("暂无记录", "No records")}</p>
      )}
      {items.map((item) => (
        <div className="recycle-row" key={item.id}>
          <span>
            <strong>
              {item.title ||
                t(
                  `${item.book_ids.length} 本书`,
                  bookCount(item.book_ids.length),
                )}
            </strong>
            <small>{dateTime(item.created_at, t("zh-CN", "en-US"))}</small>
            {item.delete_requested &&
              (item.items ?? []).map((entry) => (
                <small key={entry.book_id}>
                  {(item.items?.length ?? 0) > 1 &&
                    `${entry.title || entry.filename}: `}
                  {fileState(entry)}
                </small>
              ))}
            {item.deletion_stopped && (
              <small className="error-text">
                {item.deletion_stopped === "file_changed"
                  ? t(
                      "文件已变化，删除已停止。",
                      "File deletion stopped: the file changed.",
                    )
                  : item.deletion_stopped === "file_registered_again"
                    ? t(
                        "文件已重新登记到书库，删除已停止。",
                        "File deletion stopped: the file is in the library again.",
                      )
                    : item.deletion_stopped === "restored_from_backup"
                      ? t(
                          "恢复备份后已停止文件删除，请检查文件后再操作。",
                          "File deletion was stopped after restoring a backup. Review the files before proceeding.",
                        )
                      : t("文件删除已取消。", "File deletion was cancelled.")}
              </small>
            )}
          </span>
          {item.delete_pending && (
            <button
              disabled={!!busy}
              onClick={() =>
                void act(item.id, () =>
                  post(`/api/removals/${item.id}/cancel-deletion`, {}),
                )
              }
            >
              {t("停止待处理删除", "Stop pending deletion")}
            </button>
          )}
          {item.restored ? (
            <span>{t("已恢复", "Restored")}</span>
          ) : item.can_restore ? (
            <button
              disabled={!!busy}
              onClick={() =>
                void act(item.id, () =>
                  post(`/api/removals/${item.id}/restore`),
                )
              }
            >
              {busy === item.id && (
                <LoaderCircle className="spinner" size={16} />
              )}{" "}
              {t("恢复记录", "Restore record")}
            </button>
          ) : (
            <span className="muted">{t("文件不存在", "File unavailable")}</span>
          )}
        </div>
      ))}
      {after !== null && (
        <button
          disabled={!!busy || loading}
          onClick={() => {
            setLoading(true);
            void load(after)
              .catch(setLoadError)
              .finally(() => setLoading(false));
          }}
        >
          {t("更早的记录", "Earlier records")}
        </button>
      )}
    </section>
  );
}
