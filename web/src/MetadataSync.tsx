import { EDIT_INTENT } from "./bookActions";
import { useViewState } from "./viewState";
import { RouteLink } from "./navigation";
import type { GuardRef } from "./navigation";
import { Pill } from "./Pills";
import { LoaderCircle } from "lucide-react";
import { useEffect, useRef, useState } from "react";
import { aborted, codeMessage, errorMessage, post, request } from "./api";
import { Modal } from "./Modal";
import { CursorPagination, SkeletonRows } from "./Controls";
import { dateTime } from "./format";
import { usePolling } from "./polling";
import { useBookTitle } from "./PreferenceContext";
import type { BookTitle, Translate } from "./types";

type Mode = "pending" | "all";
type State = "pending" | "error" | "conflict" | "synced";
type Item = BookTitle & {
  id: string;
  state: State;
  pending_revision: number;
  synced_revision: number;
  error: string | null;
};
type Status = {
  last_synced_at?: number | null;
  counts: Record<State, number>;
  items: Item[];
  after: string | null;
};
type Result = { book_id: string; state: string };
type Batch = {
  items: Result[];
  after: string | null;
  through_id: string;
};
/** Books written per request; pausing takes effect between batches. */
const SYNC_BATCH = 50;
/** Books per page of the attention list. */
const LIST_PAGE = 20;
const emptyProgress = () => ({
  checked: 0,
  synced: 0,
  conflict: 0,
  error: 0,
});

export function MetadataSync({
  t,
  guard,
  navigate,
}: {
  t: Translate;
  guard: GuardRef;
  navigate: (route: string) => void;
}) {
  const title = useBookTitle();
  const [status, setStatus] = useState<Status | null>(null);
  const [busy, setBusy] = useState(false);
  const [running, setRunning] = useState(false);
  const [stopping, setStopping] = useState(false);
  const [mode, setMode] = useState<Mode | null>(null);
  const [done, setDone] = useState(false);
  const [progress, setProgress] = useState(emptyProgress);
  const [error, setError] = useState<unknown>(null);
  /** A book whose damaged sidecar file is about to be overwritten (after confirming). */
  const [overwrite, setOverwrite] = useState<Item | null>(null);
  // Cursor stack for the attention list: "" is the first page.
  const [pages, setPages] = useViewState<string[]>("sync:pages", [""]);
  const working = useRef(false),
    stop = useRef(false),
    active = useRef<AbortController | null>(null);
  const cursor = useRef({ after: "", through_id: null as string | null });
  const page = pages.at(-1) ?? "";
  const labels: Record<State, string> = {
    pending: t("待同步", "Pending"),
    error: t("同步失败", "Sync error"),
    conflict: t("外部差异待确认", "External conflict"),
    synced: t("上次已同步", "Previously synced"),
  };
  const { error: loadError } = usePolling(
    async (signal) => {
      const next = await request<Status>(
        `/api/sidecar/status?limit=${LIST_PAGE}&after=${encodeURIComponent(page)}`,
        { signal },
      );
      if (!signal.aborted) {
        if (!next.items.length && page) {
          setPages((old) => old.slice(0, -1));
          return;
        }
        setStatus(next);
      }
    },
    15_000,
    { enabled: !busy, key: pages },
  );
  useEffect(() => {
    guard.current = async () => !working.current;
    const warn = (event: BeforeUnloadEvent) => {
      if (working.current) {
        event.preventDefault();
        event.returnValue = "";
      }
    };
    window.addEventListener("beforeunload", warn);
    return () => {
      stop.current = true;
      active.current?.abort();
      guard.current = null;
      window.removeEventListener("beforeunload", warn);
    };
  }, [guard]);

  /** Rewrites a sidecar file that cannot be read from the library record. */
  const replaceInvalid = async (item: Item) => {
    if (working.current) return;
    working.current = true;
    setBusy(true);
    setError(null);
    try {
      const result = await post<{ state: string; error?: string }>(
        "/api/sidecar/sync",
        {
          book_id: item.id,
          replace_invalid: true,
        },
      );
      if (result.state === "synced" || result.state === "unchanged")
        setOverwrite(null);
      else throw new Error(result.error || result.state);
    } catch (failure) {
      setError(failure);
    } finally {
      working.current = false;
      setBusy(false);
    }
  };
  const run = async (kind: Mode, reset: boolean) => {
    if (working.current) return;
    working.current = true;
    const controller = new AbortController();
    active.current = controller;
    const signal = controller.signal;
    if (reset) {
      cursor.current = { after: "", through_id: null };
      setProgress(emptyProgress());
    }
    setBusy(true);
    setError(null);
    setMode(kind);
    setDone(false);
    setRunning(true);
    setStopping(false);
    stop.current = false;
    try {
      while (!stop.current && !signal.aborted) {
        const next = await post<Batch>(
          "/api/sidecar/sync",
          { ...cursor.current, mode: kind, limit: SYNC_BATCH },
          signal,
        );
        if (signal.aborted) return;
        setProgress((old) => {
          const value = { ...old };
          for (const result of next.items) {
            value.checked++;
            if (result.state === "synced" || result.state === "unchanged")
              value.synced++;
            else if (result.state === "conflict") value.conflict++;
            else if (result.state !== "pending" && result.state !== "removed")
              value.error++;
          }
          return value;
        });
        cursor.current = {
          after: next.after || "",
          through_id: next.through_id,
        };
        if (next.after === null) {
          setDone(true);
          break;
        }
      }
    } catch (failure) {
      if (!aborted(failure)) setError(failure);
    } finally {
      working.current = false;
      active.current = null;
      if (!signal.aborted) {
        setBusy(false);
        setRunning(false);
        setStopping(false);
      }
    }
  };
  const shown = error ?? loadError;
  return (
    <section className="metadata-sync">
      {status && (
        <div className="tags">
          {(["pending", "error", "conflict"] as State[])
            .filter((state) => status.counts[state] > 0)
            .map((state) => (
              <Pill key={state}>
                {labels[state]} {status.counts[state]}
              </Pill>
            ))}
        </div>
      )}
      {status?.last_synced_at && (
        <p className="muted small">
          {t("上次同步", "Last synced")}{" "}
          {dateTime(status.last_synced_at, t("zh-CN", "en-US"))}
        </p>
      )}
      <div className="actions">
        {!!status && status.counts.pending + status.counts.error > 0 && (
          <button
            className="primary"
            disabled={busy}
            onClick={() => run("pending", true)}
          >
            {running && <LoaderCircle size={16} className="spinner" />}
            {t("同步待处理", "Sync pending")}
          </button>
        )}
        <button disabled={busy || !status} onClick={() => run("all", true)}>
          {busy && <LoaderCircle size={16} className="spinner" />}
          {t("立即检查", "Check now")}
        </button>
        {running && (
          <button
            disabled={stopping}
            onClick={() => {
              stop.current = true;
              setStopping(true);
            }}
          >
            {t("暂停", "Pause")}
          </button>
        )}
        {!busy && mode && !done && (
          <button onClick={() => run(mode, false)}>
            {t("继续", "Continue")}
          </button>
        )}
      </div>
      {mode && (
        <div role="status" className="tags">
          <Pill>
            {t("已检查", "Checked")} {progress.checked}
          </Pill>
          <Pill>
            {t("已同步", "Synced")} {progress.synced}
          </Pill>
          <Pill>
            {t("差异", "Conflicts")} {progress.conflict}
          </Pill>
          <Pill>
            {t("失败", "Failed")} {progress.error}
          </Pill>
        </div>
      )}
      {!!shown && (
        <p role="alert" className="notice error">
          {errorMessage(shown, t)}
        </p>
      )}
      {!status && !loadError && (
        <SkeletonRows label={t("读取同步状态…", "Loading sync status…")} />
      )}
      {!!status?.items.length && (
        <div className="settings-list">
          {status.items.map((item) => (
            <div className="settings-row" key={item.id}>
              <RouteLink
                className="text-button"
                disabled={busy}
                route={`book/${item.id}`}
                onClick={() => {
                  if (item.state === "conflict") {
                    try {
                      sessionStorage.setItem(EDIT_INTENT, `review:${item.id}`);
                    } catch {
                      /* Optional storage. */
                    }
                  }
                }}
                navigate={navigate}
              >
                {title(item)}
              </RouteLink>
              <Pill>{labels[item.state]}</Pill>
              {item.state === "error" && item.error && (
                <small className="muted">{codeMessage(item.error, t)}</small>
              )}
              {item.error === "sidecar_invalid" && (
                <button
                  type="button"
                  disabled={busy}
                  onClick={() => {
                    setError(null);
                    setOverwrite(item);
                  }}
                >
                  {t("覆盖文件", "Overwrite file")}
                </button>
              )}
            </div>
          ))}
        </div>
      )}
      {overwrite && (
        <Modal
          title={t("覆盖随书信息文件？", "Overwrite the metadata file?")}
          closeLabel={t("取消", "Cancel")}
          close={() => setOverwrite(null)}
          busy={busy}
        >
          {!!error && (
            <p role="alert" className="notice error">
              {errorMessage(error, t)}
            </p>
          )}
          <p>
            {t(
              `《${title(overwrite)}》的随书信息文件已损坏、无法读取。覆盖会用书库中的记录重写它，文件里原有的内容将丢失。`,
              `The metadata file of “${title(overwrite)}” is damaged and cannot be read. Overwriting rewrites it from the library record; whatever the file held is lost.`,
            )}
          </p>
          <div className="actions modal-actions">
            <button
              type="button"
              disabled={busy}
              onClick={() => setOverwrite(null)}
            >
              {t("取消", "Cancel")}
            </button>
            <button
              type="button"
              className="danger"
              disabled={busy}
              onClick={() => void replaceInvalid(overwrite)}
            >
              {t("覆盖文件", "Overwrite file")}
            </button>
          </div>
        </Modal>
      )}
      <CursorPagination
        page={pages.length}
        more={!!status?.after}
        busy={busy}
        first={() => setPages([""])}
        previous={() => setPages((old) => old.slice(0, -1))}
        next={() => setPages((old) => [...old, status!.after!])}
        t={t}
      />
    </section>
  );
}
