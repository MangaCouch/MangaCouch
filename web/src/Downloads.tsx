import {
  activeStates,
  cancellable,
  cooldownNotices,
  downloadStates,
  resumableStates as resumable,
  retryNotice,
  terminalStates,
} from "./downloadState";
import { useRef, useState } from "react";
import { LoaderCircle, Download, Pause, Play, X } from "lucide-react";
import { Select, Segmented, ContextActions } from "./Controls";
import { Pill, languageName, languageLabel } from "./Pills";
import { errorMessage, post, request } from "./api";
import { bytes } from "./format";
import { sourceURL } from "./identifiers";
import { usePolling } from "./polling";
import { sourceMessage } from "./sourceErrors";
import type { Metadata, Translate } from "./types";
type Observation = { metadata: Metadata; fetched_at: number };
type Job = {
  id: string;
  input_url: string;
  source: any;
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
  details: {
    original?: Observation;
    selected?: Observation;
    candidates?: Observation[];
    mode?: "org" | "res";
    quote?: {
      original_gp: number | null;
      resample_gp: number | null;
      balance_gp: number | null;
    };
    duplicate?: boolean;
  };
};
/** One line of a batch submission (contract 6). */
type BatchItem = { url: string; job: Job | null; error: string | null };
type Outcome = {
  url: string;
  kind: "created" | "queued" | "invalid";
  text: string;
};
const BATCH_SIZE = 100;
const isNh = (url: string) => /(^|[/.])nhentai\.net(\/|$)/i.test(url);
const lines = (text: string) =>
  text
    .split(/\r?\n/)
    .map((line) => line.trim())
    .filter(Boolean);

export function Downloads({
  t,
  navigate,
}: {
  t: Translate;
  navigate: (route: string) => void;
}) {
  const [jobs, setJobs] = useState<Job[]>([]),
    [urls, setURLs] = useState(""),
    [entry, setEntry] = useState("find_eh"),
    [outcomes, setOutcomes] = useState<Outcome[]>([]),
    [scope, setScope] = useState("active"),
    [pages, setPages] = useState<(number | null)[]>([null]),
    [cursor, setCursor] = useState<number | null>(null),
    [total, setTotal] = useState(0),
    [cooldowns, setCooldowns] = useState<Record<string, number>>({}),
    [selected, setSelected] = useState<Set<string>>(new Set()),
    [busy, setBusy] = useState(""),
    [error, setError] = useState<unknown>(null),
    [loading, setLoading] = useState(true),
    [modes, setModes] = useState<Record<string, "org" | "res">>({}),
    [manual, setManual] = useState<Record<string, string>>({});
  const pinned = useRef<string | null>(null),
    before = pages.at(-1);
  const now = Date.now() / 1000;
  const working = jobs.some(
    (job) => activeStates.includes(job.state) && !(job.retry_at > now),
  );
  // Fast polling only while something on the active list is moving.
  const { error: refreshError, refresh } = usePolling(
    async (signal) => {
      const [result, sources] = await Promise.all([
        request<{ items: Job[]; before: number | null; total: number }>(
          `/api/downloads?scope=${scope}&limit=20${before ? `&before=${before}` : ""}`,
          { signal },
        ),
        scope === "active"
          ? request<{ cooldowns?: Record<string, number> }>(
              "/api/sources/settings",
              { signal },
            ).catch(() => null)
          : null,
      ]);
      const id = pinned.current;
      if (scope === "active" && id && !result.items.some((i) => i.id === id))
        result.items.unshift(
          await request<Job>(`/api/downloads/${encodeURIComponent(id)}`, {
            signal,
          }),
        );
      if (signal.aborted) return;
      setJobs(result.items);
      setCursor(result.before);
      setTotal(result.total);
      setCooldowns(sources?.cooldowns ?? {});
      setLoading(false);
    },
    scope !== "active" ? 60_000 : working ? 2000 : 10_000,
    { key: `${scope}:${before}` },
  );
  const nhLines = lines(urls).some(isNh);
  function mode(job: Job) {
    return (
      modes[job.id] ??
      job.details.mode ??
      (job.details.quote?.original_gp == null &&
      job.details.quote?.resample_gp != null
        ? "res"
        : "org")
    );
  }
  const actionPath = (job: Job) =>
    `/api/downloads/${encodeURIComponent(job.id)}/action`;
  /** Action failures show in the error banner; a failed list refresh afterwards shows as a refresh problem. */
  async function act(job: Job, action: string, chosenURL?: string) {
    setBusy(`${job.id}:${action}`);
    setError(null);
    try {
      const result = await post<Job>(actionPath(job), {
        action,
        revision: job.revision,
        url: chosenURL,
        mode: mode(job),
      });
      setJobs((old) =>
        old.map((item) => (item.id === result.id ? result : item)),
      );
      await refresh();
    } catch (error) {
      setError(error);
    } finally {
      setBusy("");
    }
  }
  async function batch(action: string) {
    setBusy(`batch:${action}`);
    setError(null);
    let failure: unknown = null;
    for (const job of jobs.filter((item) => selected.has(item.id))) {
      const allowed =
        action === "resume"
          ? resumable.includes(job.state)
          : action === "pause"
            ? activeStates.includes(job.state)
            : cancellable(job.state);
      if (!allowed) continue;
      try {
        await post(actionPath(job), {
          action:
            action === "resume" && job.state === "needs_attention"
              ? "recover"
              : action,
          revision: job.revision,
          mode: mode(job),
        });
      } catch (error) {
        failure ??= error;
      }
    }
    setSelected(new Set());
    if (failure) setError(failure);
    await refresh();
    setBusy("");
  }
  /** Contract 6: one request per 100 lines; invalid lines stay in the box for correction. */
  async function submit() {
    const list = lines(urls);
    if (busy || !list.length) return;
    setBusy("add");
    setError(null);
    const started = Date.now() / 1000,
      known = new Set(scope === "active" ? jobs.map((job) => job.id) : []),
      seen = new Set<string>(),
      results: Outcome[] = [],
      created: Job[] = [];
    try {
      for (let offset = 0; offset < list.length; offset += BATCH_SIZE) {
        const { items } = await post<{ items: BatchItem[] }>(
          "/api/downloads/batch",
          { urls: list.slice(offset, offset + BATCH_SIZE), entry },
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
          const existing =
            seen.has(item.job.id) ||
            known.has(item.job.id) ||
            (item.job.created_at ?? started) < started - 120;
          seen.add(item.job.id);
          if (!existing) created.push(item.job);
          results.push({
            url: item.url,
            kind: existing ? "queued" : "created",
            text: existing
              ? t("已在队列中", "Already queued")
              : t("已添加", "Added"),
          });
        }
      }
    } catch (error) {
      setError(error);
    }
    const handled = new Set(
      results.filter((r) => r.kind !== "invalid").map((r) => r.url),
    );
    setURLs(list.filter((url) => !handled.has(url)).join("\n"));
    setOutcomes(results);
    if (results.length) {
      pinned.current = seen.size === 1 ? [...seen][0]! : null;
      setJobs((old) => [...created, ...old.filter((job) => !seen.has(job.id))]);
      setScope("active");
      setPages([null]);
      await refresh();
    }
    setBusy("");
  }
  const spin = (key: string) =>
    busy === key && <LoaderCircle size={16} className="spinner" />;
  const preview = (job: Job, observation: Observation, index: number) => {
    const meta = observation.metadata,
      link = sourceURL(meta.source),
      lang = languageName(meta.language);
    return (
      <article className="source-candidate" key={index}>
        <a href={link} target="_blank" rel="noreferrer">
          <img
            loading="lazy"
            src={`/api/downloads/${job.id}/cover/${index}`}
            alt=""
          />
        </a>
        <div>
          <a href={link} target="_blank" rel="noreferrer">
            <strong>{meta.title || meta.original_title}</strong>
          </a>
          <div className="tags">
            {!["Other", "Unknown"].includes(lang) && (
              <Pill kind="language" value={lang}>
                {languageLabel(lang, t)}
              </Pill>
            )}
            {meta.declared_page_count != null && (
              <Pill>
                {meta.declared_page_count}
                {t("页", " pages")}
              </Pill>
            )}
            {meta.declared_byte_size != null && (
              <Pill>{bytes(meta.declared_byte_size)}</Pill>
            )}
            {meta.source_rating != null && (
              <Pill>★ {meta.source_rating.average}</Pill>
            )}
            {meta.uploader && <Pill>{meta.uploader}</Pill>}
          </div>
        </div>
        <button disabled={!!busy} onClick={() => void act(job, "choose", link)}>
          {spin(`${job.id}:choose`)}
          {t("选择", "Choose")}
        </button>
      </article>
    );
  };
  const count = lines(urls).length;
  const notices = cooldownNotices(cooldowns, t);
  return (
    <main className="content downloads-page">
      <span className="eyebrow">{t("下载队列", "DOWNLOADS")}</span>
      <h1>{t("下载", "Downloads")}</h1>
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
        {nhLines && (
          <Segmented
            label={t(
              "nHentai 链接的下载方式",
              "Download method for nHentai links",
            )}
            value={entry}
            change={setEntry}
            options={[
              {
                value: "find_eh",
                label: t("寻找 E(x)Hentai 版本", "Find E(x)Hentai edition"),
              },
              { value: "nh_direct", label: t("直接下载", "Download directly") },
            ]}
          />
        )}
        <div className="actions">
          <button className="primary" disabled={!!busy || !count}>
            {spin("add")}
            {count > 1
              ? t(`添加 ${count} 个链接`, `Add ${count} links`)
              : nhLines && entry === "find_eh"
                ? t("继续", "Continue")
                : t("添加", "Add")}
          </button>
          <span className="muted small">
            {t("Ctrl/⌘ + Enter 提交", "Ctrl/⌘ + Enter to submit")}
          </span>
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
            setScope(value);
            setPages([null]);
            setSelected(new Set());
            pinned.current = null;
          }}
          options={[
            { value: "active", label: t("进行中", "Active") },
            { value: "history", label: t("已结束", "History") },
          ]}
        />
        <Pill>{total}</Pill>
        <label className="checkbox">
          <input
            type="checkbox"
            checked={
              jobs.length > 0 && jobs.every((job) => selected.has(job.id))
            }
            onChange={(event) =>
              setSelected(
                event.target.checked
                  ? new Set(jobs.map((job) => job.id))
                  : new Set(),
              )
            }
          />
          {t("全选", "Select all")}
        </label>
        {selected.size > 0 && (
          <div className="actions">
            <button disabled={!!busy} onClick={() => void batch("resume")}>
              {spin("batch:resume")}
              <Play size={16} />
              {t("继续／重试", "Resume / retry")}
            </button>
            <button disabled={!!busy} onClick={() => void batch("pause")}>
              {spin("batch:pause")}
              <Pause size={16} />
              {t("暂停", "Pause")}
            </button>
            <button disabled={!!busy} onClick={() => void batch("cancel")}>
              {spin("batch:cancel")}
              <X size={16} />
              {t("取消", "Cancel")}
            </button>
          </div>
        )}
      </div>
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
        <p role="status">{t("读取下载任务…", "Loading downloads…")}</p>
      )}
      {!loading && !jobs.length && (
        <p className="muted">{t("暂无下载任务", "No downloads")}</p>
      )}
      <div className="download-list">
        {jobs.map((job) => (
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
                action: () =>
                  void act(
                    job,
                    job.state === "needs_attention" ? "recover" : "resume",
                  ),
              },
              {
                label: t("暂停", "Pause"),
                disabled: !!busy || !activeStates.includes(job.state),
                action: () => void act(job, "pause"),
              },
              {
                label: t("取消", "Cancel"),
                disabled: !!busy || !cancellable(job.state),
                action: () => void act(job, "cancel"),
              },
            ]}
          >
            <article className="download-row">
              <header>
                <input
                  type="checkbox"
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
                    {job.details.selected?.metadata.title ||
                      job.details.original?.metadata.title ||
                      job.input_url}
                  </a>
                  <div className="tags">
                    <Pill kind="source">
                      {job.source.site === "nhentai" ? "nHentai" : "E(x)Hentai"}
                    </Pill>
                    <Pill>
                      {activeStates.includes(job.state) && (
                        <LoaderCircle size={12} className="spinner" />
                      )}
                      {t(
                        ...(downloadStates[job.state] ?? [
                          job.state,
                          job.state,
                        ]),
                      )}
                    </Pill>
                    {job.state === "downloading" && (
                      <Pill>
                        {bytes(job.received)}
                        {job.total ? ` / ${bytes(job.total)}` : ""}{" "}
                        {bytes(job.bytes_per_second ?? 0)}/s
                      </Pill>
                    )}
                  </div>
                </div>
                <div className="actions">
                  {resumable.includes(job.state) && (
                    <button
                      disabled={!!busy || job.retry_at > Date.now() / 1000}
                      onClick={() =>
                        void act(
                          job,
                          job.state === "needs_attention"
                            ? "recover"
                            : "resume",
                        )
                      }
                    >
                      {spin(
                        `${job.id}:${job.state === "needs_attention" ? "recover" : "resume"}`,
                      )}
                      {job.state === "needs_attention"
                        ? t("核对远端结果", "Check remote result")
                        : t("继续／重试", "Resume / retry")}
                    </button>
                  )}
                  {activeStates.includes(job.state) && (
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
                    <button onClick={() => navigate(`book/${job.book_id}`)}>
                      {t("打开书籍", "Open book")}
                    </button>
                  )}
                  {cancellable(job.state) && (
                    <button
                      className="icon-button"
                      disabled={!!busy}
                      aria-label={t("取消任务", "Cancel task")}
                      onClick={() => void act(job, "cancel")}
                    >
                      {spin(`${job.id}:cancel`)}
                      <X size={16} />
                    </button>
                  )}
                </div>
              </header>
              {job.state === "downloading" && (
                <progress
                  value={job.total ? job.received : undefined}
                  max={job.total || 1}
                />
              )}
              {retryNotice(job, t) ? (
                <p className="muted small" role="status">
                  {retryNotice(job, t)}
                  {job.error_code && ` · ${sourceMessage(job.error_code, t)}`}
                </p>
              ) : (
                job.error_code && (
                  <p className="error-text small">
                    {sourceMessage(job.error_code, t)}
                  </p>
                )
              )}
              {job.source.site === "nhentai" &&
                [...terminalStates, "error", "needs_choice"].includes(
                  job.state,
                ) && (
                  <button
                    className="text-button"
                    disabled={!!busy}
                    onClick={() => {
                      pinned.current = job.id;
                      void act(job, "search");
                    }}
                  >
                    {spin(`${job.id}:search`)}
                    {t("寻找 E(x)Hentai 版本", "Find E(x)Hentai edition")}
                  </button>
                )}
              {job.state === "needs_choice" && (
                <div className="download-candidates">
                  <h3>nHentai</h3>
                  {job.details.original &&
                    preview(job, job.details.original, -1)}
                  <h3>{t("E(x)Hentai 候选", "E(x)Hentai candidates")}</h3>
                  {job.details.candidates?.length ? (
                    job.details.candidates.map((item, index) =>
                      preview(job, item, index),
                    )
                  ) : (
                    <p className="muted">
                      {t(
                        "暂无候选，可选择 nHentai 版本。",
                        "No candidates. You can choose the nHentai edition.",
                      )}
                    </p>
                  )}
                  <form
                    className="source-url"
                    onSubmit={(event) => {
                      event.preventDefault();
                      void act(job, "choose", manual[job.id]);
                    }}
                  >
                    <input
                      type="url"
                      required
                      value={manual[job.id] ?? ""}
                      aria-label={t("指定 E(x)Hentai 链接", "E(x)Hentai URL")}
                      placeholder={t(
                        "粘贴 E(x)Hentai 链接",
                        "Paste E(x)Hentai URL",
                      )}
                      onChange={(event) =>
                        setManual((old) => ({
                          ...old,
                          [job.id]: event.target.value,
                        }))
                      }
                    />
                    <button disabled={!!busy}>
                      {spin(`${job.id}:choose`)}
                      {t("选择", "Choose")}
                    </button>
                  </form>
                </div>
              )}
              {job.state === "needs_approval" && (
                <div className="download-approval">
                  {job.source.site !== "nhentai" && (
                    <label>
                      {t("下载质量", "Download quality")}
                      <Select
                        value={mode(job)}
                        disabled={!!busy}
                        onChange={(event) =>
                          setModes((old) => ({
                            ...old,
                            [job.id]: event.target.value as "org" | "res",
                          }))
                        }
                      >
                        <option
                          value="org"
                          disabled={job.details.quote?.original_gp == null}
                        >
                          Original (
                          {job.details.quote?.original_gp ??
                            t("未知", "Unknown")}{" "}
                          GP)
                        </option>
                        <option
                          value="res"
                          disabled={job.details.quote?.resample_gp == null}
                        >
                          Resample (
                          {job.details.quote?.resample_gp ??
                            t("未知", "Unknown")}{" "}
                          GP)
                        </option>
                      </Select>
                    </label>
                  )}
                  {job.details.quote?.balance_gp != null && (
                    <Pill>
                      {t("余额", "Balance")} {job.details.quote.balance_gp} GP
                    </Pill>
                  )}
                  <button
                    className="primary"
                    disabled={
                      !!busy ||
                      job.details.quote?.[
                        mode(job) === "org" ? "original_gp" : "resample_gp"
                      ] == null
                    }
                    onClick={() => void act(job, "approve")}
                  >
                    {spin(`${job.id}:approve`)}
                    <Download size={16} />
                    {t("确认下载", "Download")}
                    {(job.details.quote?.[
                      mode(job) === "org" ? "original_gp" : "resample_gp"
                    ] ?? 0) > 0 &&
                      ` (${job.details.quote?.[mode(job) === "org" ? "original_gp" : "resample_gp"]} GP)`}
                  </button>
                </div>
              )}
            </article>
          </ContextActions>
        ))}
      </div>
      {(pages.length > 1 || cursor !== null) && (
        <nav className="pagination">
          <button
            disabled={!!busy || pages.length === 1}
            onClick={() => setPages((old) => old.slice(0, -1))}
          >
            {t("上一页", "Previous")}
          </button>
          <Pill>{pages.length}</Pill>
          <button
            disabled={!!busy || cursor === null}
            onClick={() => setPages((old) => [...old, cursor])}
          >
            {t("下一页", "Next")}
          </button>
        </nav>
      )}
    </main>
  );
}
