import { Pill } from "./Pills";
import { LoaderCircle } from "lucide-react";
import { useEffect, useRef, useState } from "react";
import { aborted, errorMessage, post, request } from "./api";
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
  after_id: string | null;
};
type Result = { state: string };
type Batch = {
  books: Record<string, Result>;
  after_id: string | null;
  through_id: string;
};
/** Books written per request; pausing takes effect between batches. */
export const SYNC_BATCH = 50;
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
  guard: { current: (() => Promise<boolean>) | null };
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
  // Cursor stack for the attention list: "" is the first page.
  const [pages, setPages] = useState<string[]>([""]);
  const working = useRef(false),
    stop = useRef(false),
    active = useRef<AbortController | null>(null);
  const cursor = useRef({ after_id: "", through_id: null as string | null });
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
        `/api/sidecar/status?limit=50&after_id=${encodeURIComponent(page)}`,
        { signal },
      );
      if (!signal.aborted) setStatus(next);
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

  const run = async (kind: Mode, reset: boolean) => {
    if (working.current) return;
    working.current = true;
    const controller = new AbortController();
    active.current = controller;
    const signal = controller.signal;
    if (reset) {
      cursor.current = { after_id: "", through_id: null };
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
          for (const result of Object.values(next.books)) {
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
          after_id: next.after_id || "",
          through_id: next.through_id,
        };
        if (next.after_id === null) {
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
        setPages([""]);
      }
    }
  };
  const shown = error ?? loadError;
  return (
    <section className="metadata-sync">
      {status && (
        <div className="tags">
          {(["pending", "error", "conflict"] as State[]).map((state) => (
            <Pill key={state}>
              {labels[state]} {status.counts[state]}
            </Pill>
          ))}
        </div>
      )}
      {status?.last_synced_at && (
        <p className="muted small">
          {t("上次同步", "Last synced")}{" "}
          {new Date(status.last_synced_at * 1000).toLocaleString()}
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
      {status?.items.map((item) => (
        <div className="settings-row" key={item.id}>
          <button
            className="text-button"
            disabled={busy}
            onClick={() => navigate(`book/${item.id}`)}
          >
            {title(item)}
          </button>
          <Pill>{labels[item.state]}</Pill>
        </div>
      ))}
      {(pages.length > 1 || !!status?.after_id) && (
        <nav className="pagination">
          <button
            disabled={busy || pages.length === 1}
            onClick={() => setPages([""])}
          >
            {t("第一页", "First")}
          </button>
          <button
            disabled={busy || pages.length === 1}
            onClick={() => setPages((old) => old.slice(0, -1))}
          >
            {t("上一页", "Previous")}
          </button>
          <Pill>{pages.length}</Pill>
          <button
            disabled={busy || !status?.after_id}
            onClick={() => setPages((old) => [...old, status!.after_id!])}
          >
            {t("下一页", "Next page")}
          </button>
        </nav>
      )}
    </section>
  );
}
