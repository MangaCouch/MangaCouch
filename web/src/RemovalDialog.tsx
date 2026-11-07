import { useEffect, useMemo, useState } from "react";
import { LoaderCircle } from "lucide-react";
import { Modal } from "./Modal";
import { aborted, errorMessage, post, request } from "./api";
import type { Book, Translate } from "./types";
import { useBookTitle } from "./PreferenceContext";
type Preview = {
  id: string;
  book_id: string;
  token: string;
  title: string;
  blocked: string[];
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
    [deleteFile, setDeleteFile] = useState(false),
    [busy, setBusy] = useState(false),
    [error, setError] = useState<unknown>(null),
    [done, setDone] = useState<string[]>([]);
  useEffect(() => {
    const controller = new AbortController();
    void (async () => {
      if (ids.length > 1) {
        const groups: (Preview & { book_ids: string[]; items: Preview[] })[] =
          [];
        for (let offset = 0; offset < ids.length; offset += 100) {
          groups.push(
            await request<Preview & { book_ids: string[]; items: Preview[] }>(
              "/api/removals/selected/preview",
              {
                method: "POST",
                body: JSON.stringify({
                  book_ids: ids.slice(offset, offset + 100),
                }),
                signal: controller.signal,
              },
            ),
          );
        }
        if (!controller.signal.aborted) {
          setBatches(groups);
          setPreviews(groups.flatMap((group) => group.items));
        }
        return;
      }
      const identifier = ids[0];
      if (!identifier) return;
      const preview = await request<Preview>("/api/removals/preview", {
        method: "POST",
        body: JSON.stringify({ book_id: identifier }),
        signal: controller.signal,
      });
      if (!controller.signal.aborted) setPreviews([preview]);
    })().catch((error) => {
      if (!aborted(error)) setError(error);
    });
    return () => controller.abort();
  }, [ids]);
  return (
    <Modal
      title={
        ids.length === 1 && books?.[0]
          ? t(`将移除 ${title(books[0])}`, `Remove ${title(books[0])}`)
          : t(`将移除 ${ids.length} 本书`, `Remove ${ids.length} books`)
      }
      closeLabel={t("取消", "Cancel")}
      close={() => {
        if (done.length) removed(done);
        else close();
      }}
      busy={busy}
      dismissOutside={false}
    >
      {ids.length > 1 && (
        <ul className="removal-titles">
          {previews.map((item) => (
            <li key={item.book_id}>{item.title || item.book_id}</li>
          ))}
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
      {!!error && (
        <p className="notice error" role="alert">
          {errorMessage(error, t)}
        </p>
      )}
      {previews.some((item) => item.blocked.length > 0) && (
        <p className="notice">
          {t(
            "部分书籍仍有扫描或写入任务，请等待完成。",
            "Some books have pending scan or write operations.",
          )}
        </p>
      )}
      {!previews.length && !error && (
        <p role="status">{t("正在检查文件…", "Checking files…")}</p>
      )}
      <div className="actions modal-actions">
        <button
          className="danger"
          disabled={
            busy ||
            previews.length !== ids.length ||
            previews.some((item) => item.blocked.length > 0)
          }
          onClick={async () => {
            setBusy(true);
            setError(null);
            const completed = [...done];
            try {
              if (batches.length) {
                for (const batch of batches) {
                  if (batch.book_ids.every((id) => completed.includes(id)))
                    continue;
                  await post("/api/removals/selected", {
                    id: batch.id,
                    book_ids: batch.book_ids,
                    token: batch.token,
                    confirmed: true,
                    delete_file: deleteFile,
                  });
                  completed.push(...batch.book_ids);
                  setDone([...completed]);
                }
                removed(completed);
                return;
              }
              const [preview] = previews;
              if (preview && !completed.includes(preview.book_id)) {
                await post("/api/removals", {
                  id: preview.id,
                  book_id: preview.book_id,
                  token: preview.token,
                  confirmed: true,
                  delete_file: deleteFile,
                });
                completed.push(preview.book_id);
                setDone([...completed]);
              }
              removed(completed);
            } catch (error) {
              setError(error);
            } finally {
              setBusy(false);
            }
          }}
        >
          {busy && <LoaderCircle size={16} className="spinner" />}
          {t("确认移除", "Remove")}
        </button>
        <button
          disabled={busy}
          onClick={() => {
            if (done.length) removed(done);
            else close();
          }}
        >
          {t("取消", "Cancel")}
        </button>
      </div>
    </Modal>
  );
}
/** Removal receipt as listed by GET /api/removals (contract 9). */
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
    [before, setBefore] = useState<number | null>(null),
    [loading, setLoading] = useState(true),
    [busy, setBusy] = useState<string | null>(null),
    [error, setError] = useState<unknown>(null);
  /** Loads the newest page, or the page older than `cursor` and appends it. */
  async function load(cursor: number | null = null, signal?: AbortSignal) {
    const result = await request<{ items: Receipt[]; before: number | null }>(
      `/api/removals?limit=${RECYCLE_PAGE}${cursor === null ? "" : `&before=${cursor}`}`,
      { signal },
    );
    if (signal?.aborted) return;
    setItems((old) =>
      cursor === null ? result.items : [...old, ...result.items],
    );
    setBefore(result.before);
    setError(null);
  }
  useEffect(() => {
    const controller = new AbortController();
    void load(null, controller.signal)
      .catch((error) => {
        if (!aborted(error)) setError(error);
      })
      .finally(() => setLoading(false));
    return () => controller.abort();
  }, []);
  async function act(id: string, action: () => Promise<unknown>) {
    setBusy(id);
    setError(null);
    try {
      await action();
      await load();
    } catch (error) {
      setError(error);
    } finally {
      setBusy(null);
    }
  }
  const fileState = (item: NonNullable<Receipt["items"]>[number]) =>
    item.restored
      ? t("已恢复", "Restored")
      : item.file === "deleted"
        ? t("文件已删除", "File deleted")
        : t("文件保留", "File kept");
  return (
    <section>
      <p className="muted small">
        {t("记录保留 7 天", "Records are kept for 7 days")}
      </p>
      {!!error && <p className="error-text">{errorMessage(error, t)}</p>}
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
                  `${item.book_ids.length} books`,
                )}
            </strong>
            <small>{new Date(item.created_at * 1000).toLocaleString()}</small>
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
      {before !== null && (
        <button
          disabled={!!busy || loading}
          onClick={() => {
            setLoading(true);
            void load(before)
              .catch(setError)
              .finally(() => setLoading(false));
          }}
        >
          {t("更早的记录", "Earlier records")}
        </button>
      )}
    </section>
  );
}
