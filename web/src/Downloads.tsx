import { useViewState, useViewScroll } from "./viewState";
import { RouteLink } from "./navigation";
import { useTaskSummary } from "./TaskQueue";
import {
  activeStates,
  cancellable,
  clearable,
  cooldownNotices,
  downloadStates,
  downloadLabel,
  lines,
  pausable,
  resumableStates as resumable,
  retryNotice,
  terminalStates,
  withoutLines,
} from "./downloadState";
import { useEffect, useRef, useState } from "react";
import {
  LoaderCircle,
  Download,
  Pause,
  Play,
  RotateCcw,
  Trash2,
  X,
} from "lucide-react";
import {
  Pagination,
  Select,
  Segmented,
  ContextActions,
  SkeletonRows,
} from "./Controls";
import { Modal } from "./Modal";
import { BackButton } from "./navigation";
import { Pill } from "./Pills";
import { ApiError, errorMessage, post, request } from "./api";
import { bytes } from "./format";
import { SITE_LABEL, sourceURL } from "./identifiers";
import { usePolling } from "./polling";
import { sourceMessage } from "./sourceErrors";
import type { Metadata, Source, Translate } from "./types";
type Observation = { metadata: Metadata; fetched_at: number };
type Mode = "org" | "res" | "reuse";
/** A book the library already has for the gallery: the same gallery, or another edition of it. */
type LibraryMatch = { id: string; title: string; relation: "same" | "edition" };
type Job = {
  id: string;
  input_url: string;
  source: Source;
  state: string;
  revision: number;
  received: number;
  total: number | null;
  bytes_per_second?: number;
  book_id: string | null;
  error_code: string | null;
  retry_at: number;
  attempts?: number;
  created_at?: number;
  /** Set when the job comes back from a submission: new, started over, or returned as it is. */
  outcome?: "created" | "existing";
  details: {
    selected?: Observation;
    mode?: Mode;
    quote?: {
      original_gp: number | null;
      resample_gp: number | null;
      balance_gp: number | null;
    };
    /** The archiver page offers an already prepared archive of unknown quality. */
    reusable?: boolean;
    library?: LibraryMatch[];
    duplicate?: boolean;
    previous_books?: string[];
    import_error?: string;
    warnings?: string[];
    may_be_charged?: boolean;
    submission_uncertain?: boolean;
  };
};
/** One line of a batch submission. */
type FrozenApproval = {
  job: Job;
  id: string;
  expected_revision: number;
  mode: Mode;
  title: string;
  gp: number;
};
type BatchResult = {
  title?: string;
  id: string;
  ok: boolean;
  error?: string;
  job?: Job;
};
type BatchItem = { url: string; job: Job | null; error: string | null };
type Outcome = {
  url: string;
  kind: "created" | "queued" | "invalid";
  text: string;
  book?: string | null;
};
const BATCH_SIZE = 100;

export function Downloads({
  t,
  navigate,
  focusId,
}: {
  t: Translate;
  navigate: (route: string) => void;
  focusId?: string;
}) {
  const [jobs, setJobs] = useState<Job[]>([]),
    [urls, setURLs] = useState(""),
    [outcomes, setOutcomes] = useState<Outcome[]>([]),
    [scope, setScope] = useViewState("downloads:scope", "active"),
    [page, setPage] = useViewState("downloads:page", 1),
    [through, setThrough] = useViewState("downloads:through", 0),
    [total, setTotal] = useState(0),
    [selected, setSelected] = useState<Set<string>>(new Set()),
    [selectionPool, setSelectionPool] = useState<Job[] | null>(null),
    [confirmation, setConfirmation] = useState<FrozenApproval[] | null>(null),
    [batchReport, setBatchReport] = useState<{
      count: number;
      failures: BatchResult[];
    } | null>(null),
    [busy, setBusy] = useState(""),
    [error, setError] = useState<unknown>(null),
    [shown, setShown] = useState<string | null>(null),
    [modes, setModes] = useState<Record<string, Mode>>({});
  const { data: tasks } = useTaskSummary();
  const cooldowns = tasks?.cooldowns ?? {};
  const storageWaiting =
    (tasks?.downloads.storage_wait_until ?? 0) > Date.now() / 1000;
  const mutations = useRef(0);
  const locate = useRef<string | null>(focusId ?? null),
    focused = useRef<string | null>(null),
    view = `${scope}:${page}:${focusId ?? ""}`,
    // A tab or page switch shows placeholders until its own list arrives, not the old list.
    loading = shown !== view,
    listed = loading ? [] : jobs;
  useViewScroll(`downloads:${scope}:${page}`, !loading && !focusId);
  const now = Date.now() / 1000;
  useEffect(() => {
    locate.current = focusId ?? null;
    focused.current = null;
    if (!focusId) return;
    setScope("active");
    setPage(1);
    setThrough(0);
    setSelected(new Set());
    setSelectionPool(null);
  }, [focusId, setPage, setScope, setThrough]);
  useEffect(() => {
    if (!focusId || loading || focused.current === focusId) return;
    const row = document.getElementById(`download-${focusId}`);
    if (row) {
      row.scrollIntoView?.({ block: "center" });
      row.focus({ preventScroll: true });
      focused.current = focusId;
    }
  }, [focusId, loading, jobs]);
  const working = jobs.some(
    (job) => activeStates.includes(job.state) && !(job.retry_at > now),
  );
  // Fast polling only while something on the active list is moving.
  const { error: refreshError, refresh } = usePolling(
    async (signal) => {
      const generation = mutations.current;
      const result = await request<{
        items: Job[];
        total: number;
        page: number;
        through: number;
        scope: string;
      }>(
        `/api/downloads?scope=${scope}&limit=20&page=${page}${page > 1 && through > 0 ? `&through=${through}` : ""}${locate.current ? `&focus=${encodeURIComponent(locate.current)}` : ""}`,
        { signal },
      );
      if (signal.aborted || generation !== mutations.current) return;
      locate.current = null;
      if (result.page !== page || result.scope !== scope) {
        setPage(result.page);
        setScope(result.scope);
      }
      setThrough(result.through);
      setJobs(result.items);
      setSelectionPool(
        (old) =>
          old?.map(
            (job) => result.items.find((item) => item.id === job.id) ?? job,
          ) ?? null,
      );

      setTotal(result.total);
      setShown(view);
    },
    scope !== "active" ? 60_000 : working ? 2000 : 10_000,
    { key: view },
  );
  function mode(job: Job): Mode {
    return (
      modes[job.id] ??
      job.details.mode ??
      (job.details.quote?.original_gp == null &&
      job.details.quote?.resample_gp != null
        ? "res"
        : "org")
    );
  }
  /** GP the chosen archive costs; a prepared archive costs nothing more. */
  function price(job: Job) {
    const chosen = mode(job);
    if (chosen === "reuse") return job.details.reusable ? 0 : null;
    return (
      job.details.quote?.[chosen === "org" ? "original_gp" : "resample_gp"] ??
      null
    );
  }
  const jobPath = (job: Job) => `/api/downloads/${encodeURIComponent(job.id)}`;
  const actionPath = (job: Job) =>
    `${jobPath(job)}/action?expected_revision=${job.revision}`;
  /** Action failures show in the error banner; a failed list refresh afterwards shows as a refresh problem. */
  async function act(job: Job, action: string) {
    mutations.current++;
    setBusy(`${job.id}:${action}`);
    setError(null);
    try {
      const result = await post<Job>(actionPath(job), {
        action,
        mode: mode(job),
      });
      mutations.current++;
      setJobs((old) =>
        old.map((item) => (item.id === result.id ? result : item)),
      );
      setModes((old) => {
        const next = { ...old };
        delete next[job.id];
        return next;
      });
      await refresh();
    } catch (error) {
      setError(error);
    } finally {
      setBusy("");
    }
  }
  /** Forgets a finished or failed job (the book it imported stays). */
  async function clear(job: Job) {
    mutations.current++;
    setBusy(`${job.id}:clear`);
    setError(null);
    try {
      await request(jobPath(job), { method: "DELETE" });

      mutations.current++;
      setJobs((old) => old.filter((item) => item.id !== job.id));
      setSelected((old) => {
        const next = new Set(old);
        next.delete(job.id);
        return next;
      });
      await refresh();
    } catch (error) {
      setError(error);
    } finally {
      setBusy("");
    }
  }
  async function batch(action: string, frozen?: FrozenApproval[]) {
    const targets =
      frozen ??
      chosen
        .filter((job) => eligible(job, action))
        .map((job) => ({
          job,
          id: job.id,
          expected_revision: job.revision,
          mode: mode(job),
          title: job.details.selected?.metadata.title || job.input_url,
          gp: price(job) ?? 0,
        }));
    if (!targets.length) return;
    if (action === "approve" && !frozen) {
      setConfirmation(targets);
      return;
    }
    mutations.current++;
    setBusy(`batch:${action}`);
    setError(null);
    setBatchReport(null);
    const results: BatchResult[] = [];
    try {
      for (let offset = 0; offset < targets.length; offset += 500) {
        const response = await post<{ results: BatchResult[] }>(
          "/api/downloads/actions",
          {
            action: action === "clear" ? "delete" : action,
            items: targets
              .slice(offset, offset + 500)
              .map(({ id, expected_revision, mode }) => ({
                id,
                expected_revision,
                mode,
              })),
          },
        );
        results.push(...response.results);
      }
      const failures = results
        .filter((item) => !item.ok)
        .map((item) => ({
          ...item,
          title: targets.find((target) => target.id === item.id)?.title,
        }));
      setBatchReport({ count: results.length - failures.length, failures });
      mutations.current++;
      const succeeded = new Set(
        results.filter((item) => item.ok).map((item) => item.id),
      );
      const remaining = [
        ...new Map(
          [...chosen, ...targets.map((item) => item.job)].map((job) => [
            job.id,
            job,
          ]),
        ).values(),
      ].filter((job) => !succeeded.has(job.id));
      setSelected(new Set(remaining.map((job) => job.id)));
      setSelectionPool(
        remaining.map(
          (job) => failures.find((item) => item.id === job.id)?.job ?? job,
        ),
      );
      setConfirmation(null);
      await refresh();
    } catch (failure) {
      mutations.current++;
      const succeeded = new Set(
        results.filter((item) => item.ok).map((item) => item.id),
      );
      const remaining = [
        ...new Map(
          [...chosen, ...targets.map((item) => item.job)].map((job) => [
            job.id,
            job,
          ]),
        ).values(),
      ].filter((job) => !succeeded.has(job.id));
      setSelected(new Set(remaining.map((job) => job.id)));
      setSelectionPool(remaining);
      setConfirmation(null);
      setError(failure);
    } finally {
      setBusy("");
    }
  }
  async function queryBalance() {
    setBusy("balance");
    try {
      await post("/api/sources/balance", {});
      await refresh();
    } catch (failure) {
      setError(failure);
    } finally {
      setBusy("");
    }
  }
  async function selectQueue() {
    setBusy("select");
    setError(null);
    try {
      const snapshot = new Map<string, Job>();
      let after: string | null = null;
      do {
        const page: { items: Job[]; after: string | null } = await request(
          `/api/downloads?scope=${scope}&limit=100${after ? `&after=${encodeURIComponent(after)}` : ""}`,
        );
        for (const job of page.items) snapshot.set(job.id, job);
        after = page.after;
      } while (after);
      setSelectionPool([...snapshot.values()]);
      setSelected(new Set(snapshot.keys()));
    } catch (error) {
      setError(error);
    } finally {
      setBusy("");
    }
  }
  /** One request per 100 lines; invalid lines stay in the box for correction. */
  async function submit() {
    const list = lines(urls);
    if (busy || !list.length) return;
    setBusy("add");
    setError(null);
    const seen = new Set<string>(),
      results: Outcome[] = [];
    try {
      for (let offset = 0; offset < list.length; offset += BATCH_SIZE) {
        const { items } = await post<{ items: BatchItem[] }>(
          "/api/downloads/batch",
          { urls: list.slice(offset, offset + BATCH_SIZE) },
        );
        for (const item of items) {
          if (!item.job) {
            results.push({
              url: item.url,
              kind: "invalid",
              text: sourceMessage(item.error ?? "invalid_url", t),
            });
            continue;
          }
          // The server says whether the gallery is new; E-Hentai and ExHentai links share a job.
          const repeated = seen.has(item.job.id),
            fresh = !repeated && item.job.outcome !== "existing";
          seen.add(item.job.id);
          const done = item.job.state === "done",
            [zh, en] = downloadStates[item.job.state] ?? [
              item.job.state,
              item.job.state,
            ];
          results.push({
            url: item.url,
            kind: fresh ? "created" : "queued",
            text: fresh
              ? t("已添加", "Added")
              : done
                ? t(
                    "已下载过，书在书库中；如需再次下载，请在“已结束”中选择“重新下载”",
                    "Already downloaded; the book is in the library. Use Download again under History to fetch it again",
                  )
                : item.job.state === "cancelled"
                  ? t(
                      "已有这个下载任务（已取消）；如需继续，请在“已结束”中选择“重试”",
                      "Already in the list (Cancelled); use Retry under History to start it again",
                    )
                  : activeStates.includes(item.job.state)
                    ? t("已在队列中", "Already queued")
                    : t(
                        `已有这个下载任务（${zh}），请在下方列表中处理`,
                        `Already in the list (${en}); continue it below`,
                      ),
            book: done ? item.job.book_id : null,
          });
        }
      }
    } catch (error) {
      setError(error);
    }
    // Only the submitted lines the server took leave the box; links pasted meanwhile stay.
    const handled = results
      .filter((r) => r.kind !== "invalid")
      .map((r) => r.url);
    setURLs((current) => withoutLines(current, handled));
    setOutcomes(results);
    if (results.length) {
      await refresh();
    }
    setBusy("");
  }
  const spin = (key: string) =>
    busy === key && <LoaderCircle size={16} className="spinner" />;
  const count = lines(urls).length;
  const notices = cooldownNotices(cooldowns, t);
  const chosen = (selectionPool ?? listed).filter(
    (job) =>
      selected.has(job.id) &&
      (scope === "active"
        ? !terminalStates.includes(job.state)
        : terminalStates.includes(job.state)),
  );
  const canClear = (job: Job) =>
    clearable(job.state) &&
    !(job.state === "error" && job.details.submission_uncertain);
  function eligible(job: Job, action: string) {
    if (action === "clear") return canClear(job);
    if (action === "pause") return pausable(job.state);
    if (action === "cancel") return cancellable(job.state);
    if (action === "resume") return resumable.includes(job.state);
    if (action === "redownload") return job.state === "cancelled";
    return job.state === "needs_approval" && price(job) != null;
  }
  const approvals = chosen.filter(
    (job) => job.state === "needs_approval" && price(job) != null,
  );
  const totalGP = approvals.reduce((sum, job) => sum + (price(job) ?? 0), 0);
  const balances = approvals.flatMap((job) =>
    job.details.quote?.balance_gp == null ? [] : [job.details.quote.balance_gp],
  );
  const lowBalance = balances.length > 0 && totalGP > Math.min(...balances);
  const turn = (next: number) => {
    if (!selectionPool) setSelected(new Set());
    setPage(next);
    if (next === 1) setThrough(0);
  };
  const libraryNotice = (job: Job) =>
    !!job.details.library?.length && (
      <div className="notice small" role="status">
        {job.details.library.some((item) => item.relation === "same")
          ? t(
              "书库中已有这个作品，确认下载前请先查看：",
              "The library already has this gallery. Check before downloading:",
            )
          : t(
              "书库中有这个作品的其他版本：",
              "The library has another edition of this gallery:",
            )}{" "}
        {job.details.library.map((item) => (
          <RouteLink
            key={item.id}
            className="text-button"
            route={`book/${item.id}`}
            navigate={navigate}
          >
            {item.title}
            {item.relation === "edition" && ` (${t("其他版本", "edition")})`}
          </RouteLink>
        ))}
      </div>
    );
  return (
    <main className="content downloads-page">
      <BackButton t={t} />
      <h1>{t("下载", "Downloads")}</h1>
      {tasks?.downloads.blocked && (
        <p className="notice error" role="alert">
          {sourceMessage(tasks.downloads.blocked, t)}{" "}
          <RouteLink route="settings" navigate={navigate}>
            {t("打开设置", "Open settings")}
          </RouteLink>
        </p>
      )}
      {storageWaiting && (
        <p className="notice" role="status">
          {t(
            "部分下载因磁盘空间不足而等待，每 5 分钟检查并自动恢复；其他任务可继续。",
            "Some downloads are waiting for disk space, checked every 5 minutes; other tasks can continue.",
          )}
        </p>
      )}
      <form
        className="panel source-add"
        onSubmit={(event) => {
          event.preventDefault();
          void submit();
        }}
      >
        <textarea
          className="batch-urls"
          rows={Math.min(8, Math.max(2, count + 1))}
          value={urls}
          spellCheck={false}
          aria-label={t("作品链接（每行一个）", "Gallery URLs (one per line)")}
          placeholder={t(
            "粘贴作品链接，每行一个",
            "Paste gallery URLs, one per line",
          )}
          onChange={(event) => setURLs(event.target.value)}
          onKeyDown={(event) => {
            if (event.key === "Enter" && (event.ctrlKey || event.metaKey)) {
              event.preventDefault();
              void submit();
            }
          }}
        />
        <div className="actions">
          <button
            className="primary"
            disabled={!!busy || !count}
            aria-keyshortcuts="Control+Enter Meta+Enter"
            title={t("Ctrl/⌘ + Enter 提交", "Ctrl/⌘ + Enter to submit")}
          >
            {spin("add")}
            {count > 1
              ? t(`添加 ${count} 个链接`, `Add ${count} links`)
              : t("添加", "Add")}
          </button>
        </div>
        {outcomes.length > 0 && (
          <ul className="batch-results" role="status">
            {outcomes.map((item, index) => (
              <li key={`${index}:${item.url}`} className={item.kind}>
                <span>{item.url}</span>
                <span
                  className={
                    item.kind === "invalid" ? "error-text small" : "muted small"
                  }
                >
                  {item.text}
                </span>
                {item.book && (
                  <RouteLink
                    className="text-button"
                    route={`book/${item.book}`}
                    navigate={navigate}
                  >
                    {t("打开书籍", "Open book")}
                  </RouteLink>
                )}
              </li>
            ))}
          </ul>
        )}
      </form>
      {notices.map((text) => (
        <p key={text} className="notice" role="status">
          {text}
        </p>
      ))}
      <div className="download-tools">
        <Segmented
          label={t("下载列表", "Downloads")}
          value={scope}
          change={(value) => {
            if (busy) return;
            setScope(value);
            setPage(1);
            setThrough(0);
            setSelected(new Set());
            setSelectionPool(null);
            locate.current = null;
          }}
          options={[
            { value: "active", label: t("进行中", "Active") },
            { value: "history", label: t("已结束", "History") },
          ]}
        />
        {(loading || total > 0) && <Pill>{loading ? "…" : total}</Pill>}
        {listed.length > 0 && (
          <label className="checkbox">
            <input
              type="checkbox"
              disabled={!!busy || loading}
              checked={
                listed.length > 0 && listed.every((job) => selected.has(job.id))
              }
              onChange={(event) => {
                setSelectionPool(null);
                setSelected(
                  event.target.checked
                    ? new Set(listed.map((job) => job.id))
                    : new Set(),
                );
              }}
            />
            {t("全选本页", "Select page")}
          </label>
        )}
        {total > listed.length && (
          <button
            disabled={!!busy || loading}
            onClick={() => void selectQueue()}
          >
            {spin("select")}
            {t(`选择全部 ${total} 项`, `Select all ${total} tasks`)}
          </button>
        )}
        {chosen.length > 0 && (
          <span className="muted small" role="status">
            {t(`已选 ${chosen.length} 项`, `${chosen.length} selected`)}
          </span>
        )}
        {chosen.length > 0 && scope === "active" && (
          <div className="actions">
            {approvals.length > 0 && (
              <button
                className="primary"
                disabled={!!busy}
                onClick={() => void batch("approve")}
              >
                {spin("batch:approve")}
                <Download size={16} />
                {t(
                  `确认下载 ${approvals.length} 项`,
                  `Confirm ${approvals.length} downloads`,
                )}{" "}
                · {totalGP.toLocaleString()} GP
              </button>
            )}
            {chosen.some((job) => job.state === "error" && canClear(job)) && (
              <button
                disabled={
                  !!busy || !chosen.some((job) => eligible(job, "clear"))
                }
                onClick={() => void batch("clear")}
              >
                {spin("batch:clear")}
                <Trash2 size={16} />
                {t("清除失败任务", "Clear failed tasks")}
              </button>
            )}
            <button
              disabled={
                !!busy || !chosen.some((job) => eligible(job, "resume"))
              }
              onClick={() => void batch("resume")}
            >
              {spin("batch:resume")}
              <Play size={16} />
              {t("继续／重试", "Resume / retry")}
            </button>
            <button
              disabled={!!busy || !chosen.some((job) => eligible(job, "pause"))}
              onClick={() => void batch("pause")}
            >
              {spin("batch:pause")}
              <Pause size={16} />
              {t("暂停", "Pause")}
            </button>
            <button
              disabled={
                !!busy || !chosen.some((job) => eligible(job, "cancel"))
              }
              onClick={() => void batch("cancel")}
            >
              {spin("batch:cancel")}
              <X size={16} />
              {t("取消", "Cancel")}
            </button>
          </div>
        )}
        {chosen.length > 0 && scope === "history" && (
          <div className="actions">
            {chosen.some((job) => job.state === "cancelled") && (
              <button
                disabled={
                  !!busy || !chosen.some((job) => eligible(job, "redownload"))
                }
                onClick={() => void batch("redownload")}
              >
                {spin("batch:redownload")}
                <RotateCcw size={16} />
                {t("重试", "Retry")}
              </button>
            )}
            <button
              disabled={!!busy || !chosen.some((job) => eligible(job, "clear"))}
              onClick={() => void batch("clear")}
            >
              {spin("batch:clear")}
              <Trash2 size={16} />
              {t("清除记录", "Clear records")}
            </button>
          </div>
        )}
      </div>
      {selectionPool && chosen.length > 0 && (
        <p className="muted small" role="status">
          {t(
            "已跨页选择；之后新增的任务不会自动选中。",
            "Selected across pages; newly added tasks are not selected.",
          )}
          <button
            className="text-button"
            disabled={!!busy}
            onClick={() => {
              setSelected(new Set());
              setSelectionPool(null);
            }}
          >
            {t("取消选择", "Clear selection")}
          </button>
        </p>
      )}
      {chosen.some(
        (job) => job.state === "needs_approval" && price(job) == null,
      ) && (
        <p className="notice small" role="status">
          {t(
            "价格未知的任务不计入总额，也不会批量确认。",
            "Tasks with unknown prices are excluded from the total and bulk confirmation.",
          )}
        </p>
      )}
      {lowBalance && (
        <p className="notice" role="status">
          {t(
            "GP 余额不足，仍可确认下载；余额用完后自动等待，充值后继续。",
            "Not enough GP. You can still confirm; downloads wait when funds run out and continue after topping up.",
          )}
        </p>
      )}
      {!!error && (
        <p role="alert" className="notice error">
          {errorMessage(error, t)}
        </p>
      )}
      {!!refreshError && (
        <p role="alert" className="notice error">
          {t("下载列表刷新失败：", "Could not refresh the downloads: ")}
          {errorMessage(refreshError, t)}
        </p>
      )}
      {loading && !refreshError && (
        <SkeletonRows label={t("读取下载任务…", "Loading downloads…")} />
      )}
      {!loading && !jobs.length && (
        <p className="muted">{t("暂无下载任务", "No downloads")}</p>
      )}
      <div className="download-list">
        {listed.map((job) => (
          <ContextActions
            key={job.id}
            actions={[
              {
                label: t("打开来源", "Open source"),
                action: () =>
                  window.open(
                    sourceURL(job.source),
                    "_blank",
                    "noopener,noreferrer",
                  ),
              },
              {
                label: t("继续／重试", "Resume / retry"),
                disabled: !!busy || !resumable.includes(job.state),
                action: () => void act(job, "resume"),
              },
              {
                label: t("暂停", "Pause"),
                disabled: !!busy || !pausable(job.state),
                action: () => void act(job, "pause"),
              },
              {
                label: t("取消", "Cancel"),
                disabled: !!busy || !cancellable(job.state),
                action: () => void act(job, "cancel"),
              },
              {
                label: t("重新下载", "Download again"),
                disabled: !!busy || !terminalStates.includes(job.state),
                action: () => void act(job, "redownload"),
              },
              {
                label: t("清除记录", "Clear record"),
                disabled: !!busy || !canClear(job),
                action: () => void clear(job),
              },
            ]}
          >
            <article
              className="download-row"
              id={`download-${job.id}`}
              tabIndex={-1}
              aria-current={focusId === job.id ? "true" : undefined}
            >
              <header>
                <input
                  type="checkbox"
                  disabled={!!busy}
                  checked={selected.has(job.id)}
                  aria-label={t("选择任务", "Select task")}
                  onChange={(event) =>
                    setSelected((old) => {
                      const next = new Set(old);
                      if (event.target.checked) next.add(job.id);
                      else next.delete(job.id);
                      return next;
                    })
                  }
                />
                <div className="download-title">
                  <a
                    href={sourceURL(job.source)}
                    target="_blank"
                    rel="noreferrer"
                  >
                    {job.details.selected?.metadata.title || job.input_url}
                  </a>
                  <div className="tags">
                    <Pill kind="source">{SITE_LABEL}</Pill>
                    <Pill>
                      {activeStates.includes(job.state) && (
                        <LoaderCircle size={12} className="spinner" />
                      )}
                      {downloadLabel(job, t)}
                    </Pill>
                    {job.state === "downloading" && (
                      <Pill>
                        {bytes(job.received)}
                        {job.total ? ` / ${bytes(job.total)}` : ""}{" "}
                        {bytes(job.bytes_per_second ?? 0)}/s
                      </Pill>
                    )}
                  </div>
                  {job.state === "downloading" && (
                    <progress
                      value={job.total ? job.received : undefined}
                      max={job.total || 1}
                    />
                  )}
                </div>
                <div className="actions">
                  {resumable.includes(job.state) && (
                    <button
                      disabled={!!busy}
                      onClick={() => void act(job, "resume")}
                    >
                      {spin(`${job.id}:${"resume"}`)}
                      {t("继续／重试", "Resume / retry")}
                    </button>
                  )}
                  {!terminalStates.includes(job.state) &&
                    job.details.may_be_charged && (
                      <p className="notice">
                        {t(
                          "上一次请求可能已扣费",
                          "The previous request may already have been charged",
                        )}
                      </p>
                    )}
                  {job.error_code === "insufficient_gp" && (
                    <button
                      disabled={!!busy}
                      onClick={() => void queryBalance()}
                    >
                      {t("立即查询余额", "Check balance now")}
                    </button>
                  )}
                  {pausable(job.state) && (
                    <button
                      className="icon-button"
                      disabled={!!busy}
                      aria-label={t("暂停", "Pause")}
                      onClick={() => void act(job, "pause")}
                    >
                      {spin(`${job.id}:pause`)}
                      <Pause size={16} />
                    </button>
                  )}
                  {job.state === "done" && job.book_id && (
                    <RouteLink
                      route={`book/${job.book_id}`}
                      navigate={navigate}
                    >
                      {t("打开书籍", "Open book")}
                    </RouteLink>
                  )}
                  {terminalStates.includes(job.state) && (
                    <button
                      disabled={!!busy}
                      onClick={() => void act(job, "redownload")}
                    >
                      {spin(`${job.id}:redownload`)}
                      {job.state === "cancelled"
                        ? t("重试", "Retry")
                        : t("重新下载", "Download again")}
                    </button>
                  )}
                  {cancellable(job.state) && (
                    <button
                      className="icon-button close-button"
                      disabled={!!busy}
                      aria-label={t("取消任务", "Cancel task")}
                      onClick={() => void act(job, "cancel")}
                    >
                      {spin(`${job.id}:cancel`)}
                      <X size={14} />
                    </button>
                  )}
                  {canClear(job) && (
                    <button
                      className="icon-button"
                      disabled={!!busy}
                      aria-label={t("清除记录", "Clear record")}
                      onClick={() => void clear(job)}
                    >
                      {spin(`${job.id}:clear`)}
                      <Trash2 size={16} />
                    </button>
                  )}
                </div>
              </header>
              {retryNotice(job, t) ? (
                <p className="muted small" role="status">
                  {retryNotice(job, t)}
                  {job.error_code &&
                    ` · ${sourceMessage(job.error_code ?? "", t, true)}`}
                </p>
              ) : (
                job.error_code && (
                  <p className="error-text small">
                    {sourceMessage(job.error_code ?? "", t)}
                    {job.error_code === "download_invalid_archive" &&
                      job.details.import_error &&
                      ` (${job.details.import_error})`}
                  </p>
                )
              )}
              {job.details.warnings?.includes("source_metadata_not_saved") && (
                <p className="muted small">
                  {t("来源信息稍后补写", "Source details will be saved later")}
                </p>
              )}
              {job.state === "done" &&
                (job.details.duplicate ||
                  !!job.details.previous_books?.length) && (
                  <p className="muted small">
                    {job.details.duplicate &&
                      t(
                        "书库中已有相同文件，没有新增书籍。",
                        "The library already had this file; no new book was added.",
                      )}{" "}
                    {job.details.previous_books?.map((id, index) => (
                      <RouteLink
                        key={id}
                        className="text-button"
                        route={`book/${id}`}
                        navigate={navigate}
                      >
                        {t(
                          `其他版本 ${index + 1}`,
                          `Other edition ${index + 1}`,
                        )}
                      </RouteLink>
                    ))}
                  </p>
                )}
              {job.state === "needs_approval" && libraryNotice(job)}
              {job.state === "needs_approval" && (
                <div className="download-approval">
                  <label>
                    {t("下载质量", "Download quality")}
                    <Select
                      value={mode(job)}
                      disabled={!!busy}
                      onChange={(event) =>
                        setModes((old) => ({
                          ...old,
                          [job.id]: event.target.value as Mode,
                        }))
                      }
                    >
                      {job.details.reusable && (
                        <option value="reuse">
                          {t(
                            "已准备好的归档（质量未知，0 GP）",
                            "Prepared archive (quality unknown, 0 GP)",
                          )}
                        </option>
                      )}
                      <option
                        value="org"
                        disabled={job.details.quote?.original_gp == null}
                      >
                        {t("原图", "Original")} (
                        {job.details.quote?.original_gp ?? t("未知", "Unknown")}{" "}
                        GP)
                      </option>
                      <option
                        value="res"
                        disabled={job.details.quote?.resample_gp == null}
                      >
                        {t("重采样", "Resample")} (
                        {job.details.quote?.resample_gp ?? t("未知", "Unknown")}{" "}
                        GP)
                      </option>
                    </Select>
                  </label>
                  {job.details.quote?.balance_gp != null && (
                    <Pill>
                      {t("余额", "Balance")} {job.details.quote.balance_gp} GP
                    </Pill>
                  )}
                  {job.details.quote?.balance_gp != null &&
                    price(job) != null &&
                    price(job)! > job.details.quote.balance_gp && (
                      <span className="muted small">
                        {t(
                          "GP 不足仍可确认，充值后自动继续。",
                          "You can confirm with insufficient GP; downloads resume after topping up.",
                        )}
                      </span>
                    )}
                  <button
                    className="primary"
                    disabled={!!busy || price(job) == null}
                    onClick={() => void act(job, "approve")}
                  >
                    {spin(`${job.id}:approve`)}
                    <Download size={16} />
                    {t("确认下载", "Download")}
                    {(price(job) ?? 0) > 0 && ` (${price(job)} GP)`}
                  </button>
                </div>
              )}
            </article>
          </ContextActions>
        ))}
      </div>
      {batchReport && (
        <div role="status" className="notice">
          {t(
            `已完成 ${batchReport.count} 个，${batchReport.failures.length} 个失败`,
            `${batchReport.count} completed, ${batchReport.failures.length} failed`,
          )}
          {batchReport.failures.map((item) => (
            <p key={item.id}>
              {item.title ?? item.id}:{" "}
              {errorMessage(new ApiError(409, item.error ?? null), t)}
            </p>
          ))}
        </div>
      )}
      {confirmation && (
        <Modal
          title={t("确认批量下载", "Confirm downloads")}
          closeLabel={t("取消", "Cancel")}
          close={() => setConfirmation(null)}
          busy={!!busy}
        >
          <ul>
            {confirmation.map((item) => (
              <li key={item.id}>
                {item.title} — {item.gp} GP
              </li>
            ))}
          </ul>
          <p>
            {t("总计", "Total")}:{" "}
            {confirmation.reduce((sum, item) => sum + item.gp, 0)} GP
          </p>
          <button
            disabled={!!busy}
            onClick={() => void batch("approve", confirmation)}
          >
            {t("确认下载", "Confirm download")}
          </button>
          <button disabled={!!busy} onClick={() => setConfirmation(null)}>
            {t("取消", "Cancel")}
          </button>
        </Modal>
      )}
      <Pagination
        page={page}
        total={total}
        size={20}
        busy={!!busy || loading}
        change={turn}
        t={t}
      />
    </main>
  );
}
