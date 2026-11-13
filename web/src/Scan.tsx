import { RouteLink } from "./navigation";
import { InfoTip } from "./InfoTip";
import { useEffect, useRef, useState } from "react";
import { SkeletonRows } from "./Controls";
import { useAction } from "./action";
import { FolderSearch, Pause } from "lucide-react";
import { codeMessage, errorMessage, request } from "./api";
import { Pill } from "./Pills";
import { usePolling } from "./polling";
import type { Translate } from "./types";

type ItemState =
  "pending" | "expanded" | "indexed" | "unchanged" | "duplicate" | "error";
interface ScanItem {
  id: number;
  path: string;
  kind: "directory" | "archive";
  state: ItemState;
  book_id: string | null;
  error_code: string | null;
  error_message?: string | null;
  warnings: string[];
  previous_books?: string[];
}
interface ScanRun {
  id: string;
  state: "active" | "paused" | "done";
  keep_source: boolean;
  created_at: number;
  ignored: number;
  error_code: string | null;
  counts: Record<ItemState, number>;
  archives: number;
  with_metadata: number;
  without_metadata: number;
  processed: number;
  items: ScanItem[];
  after: string | null;
}

/** Rows per "Load more" page, and the largest page the server answers. */
const PAGE = 50,
  MAX_PAGE = 200;

export function Scan({
  t,
  navigate,
}: {
  t: Translate;
  navigate: (route: string) => void;
}) {
  const [scan, setScan] = useState<ScanRun | null>(null),
    [root, setRoot] = useState("");
  const visibleScan = useRef(scan);
  visibleScan.current = scan;
  const [keepSource, setKeepSource] = useState(false);
  const [failedOnly, setFailedOnly] = useState(false);
  const [notice, setNotice] = useState("");
  const action = useAction();
  const { busy, error } = action;
  const [loaded, setLoaded] = useState(false);
  const filterChanged = useRef(false);
  const command = useRef<AbortController | null>(null);
  useEffect(() => () => command.current?.abort(), []);
  // Follows a running scan closely; otherwise only checks now and then.
  const { error: loadError } = usePolling(
    async (signal) => {
      const filter = failedOnly ? "&state=error" : "";
      const old = filterChanged.current ? null : visibleScan.current;
      // Refreshes every loaded row (and one more, to find rows added after an exhausted
      // cursor) in as few requests as the server's 200-row pages allow.
      const keep = old
        ? Math.max(PAGE, old.items.length + (old.after === null ? 1 : 0))
        : PAGE;
      const page = (have: number) =>
        Math.min(MAX_PAGE, Math.ceil((keep - have) / PAGE) * PAGE);
      // A finished scan never changes again: one small request confirms it is still the
      // latest, and the rows already shown are kept.
      const finished = old?.state === "done";
      const data = await request<{ scan: ScanRun | null; root: string }>(
        `/api/scans?limit=${finished ? 1 : page(0)}${filter}`,
        { signal },
      );
      if (signal.aborted) return;
      filterChanged.current = false;
      let value: ScanRun | null = data.scan;
      if (finished && value?.id === old.id && value.state === "done") {
        value = { ...value, items: old.items, after: old.after };
      } else if (finished && value) {
        value = (
          await request<{ scan: ScanRun | null }>(
            `/api/scans?limit=${PAGE}${filter}`,
            { signal },
          )
        ).scan;
      } else if (old && value && old.id === value.id) {
        while (value && value.after && value.items.length < keep) {
          const more: ScanRun = await request<ScanRun>(
            `/api/scans/${value.id}?after=${encodeURIComponent(value.after)}&limit=${page(value.items.length)}${filter}`,
            { signal },
          );
          value = { ...more, items: [...value.items, ...more.items] };
        }
      }
      if (signal.aborted) return;
      setScan(value);
      setRoot(data.root);
      setLoaded(true);
      if (data.scan && data.scan.state !== "done")
        setKeepSource(data.scan.keep_source);
    },
    scan?.state === "active" ? 1500 : 15_000,
    { enabled: !busy, key: failedOnly },
  );
  const loading = !loaded && !loadError;
  async function act(task: (signal: AbortSignal) => Promise<void>) {
    if (busy) return;
    const controller = new AbortController();
    command.current = controller;
    setNotice("");
    await action.run(() => task(controller.signal));
  }
  const start = () =>
    void act(async (signal) => {
      const data = await request<ScanRun>(
        scan && scan.state !== "done"
          ? `/api/scans/${scan.id}/resume`
          : `/api/scans?keep_source=${keepSource}`,
        { method: "POST", signal },
      );
      if (signal.aborted) return;
      setScan(data);
    });
  const pause = () => {
    if (!scan) return;
    void act(async (signal) => {
      const data = await request<ScanRun>(`/api/scans/${scan.id}/pause`, {
        method: "POST",
        signal,
      });
      if (signal.aborted) return;
      setScan(data);
      setNotice(
        t("正在暂停，当前文件完成后停止。", "Pausing after the current file."),
      );
    });
  };
  const labels: Record<ItemState, string> = {
    pending: t("待处理", "Pending"),
    expanded: t("目录已展开", "Folder listed"),
    indexed: t("已登记", "Indexed"),
    unchanged: t("文件未变", "Unchanged"),
    duplicate: t("重复文件", "Duplicate"),
    error: t("需处理", "Needs attention"),
  };
  const reason = (item: ScanItem) =>
    codeMessage(item.error_code, t) ??
    item.error_message ??
    t(
      "处理失败，可检查后重新扫描。",
      "Processing failed. Check the file and scan again.",
    );
  return (
    <section className="scan">
      {!!root && (
        <p className="scan-root small">
          <FolderSearch size={16} />
          <span>{root}</span>
        </p>
      )}
      {!!(error ?? loadError) && (
        <p className="notice error" role="alert">
          {errorMessage(error ?? loadError, t)}
        </p>
      )}
      {notice && (
        <p className="notice" role="status">
          {notice}
        </p>
      )}
      {scan?.state === "paused" && !!scan.error_code && (
        <p className="notice error" role="alert">
          {codeMessage(scan.error_code, t)}
        </p>
      )}
      <label className="checkbox">
        <input
          type="checkbox"
          checked={keepSource}
          disabled={(!!scan && scan.state !== "done") || busy}
          onChange={(event) => setKeepSource(event.target.checked)}
        />
        {t("导入后保留源文件", "Keep source files after import")}
      </label>
      <div className="actions scan-actions">
        <button
          className="primary"
          disabled={busy || loading || scan?.state === "active" || !root}
          onClick={start}
        >
          <FolderSearch size={17} />
          {scan && scan.state !== "done"
            ? t("继续扫描", "Continue scan")
            : t("开始扫描", "Start scan")}
        </button>
        {scan && scan.state !== "done" && (
          <button disabled={busy || scan.state === "paused"} onClick={pause}>
            <Pause size={16} />
            {t("暂停扫描", "Pause scan")}
          </button>
        )}
        <InfoTip label={t("关于继续扫描", "About continuing a scan")}>
          {t(
            "修复后再次开始扫描，已登记且未变的文件会跳过。",
            "Fix any failed files and start another scan; indexed, unchanged files will be skipped.",
          )}
        </InfoTip>
      </div>
      {loading && (
        <SkeletonRows label={t("读取扫描记录…", "Loading scan records…")} />
      )}
      {scan && (
        <div className="scan-results">
          <div className="section-head">
            <h4>
              {scan.state === "active"
                ? t("扫描中", "Scanning")
                : scan.state === "done"
                  ? scan.counts.error
                    ? t(
                        "扫描结束，有项目需处理",
                        "Scan finished with items needing attention",
                      )
                    : t("扫描完成", "Scan complete")
                  : t("已暂停", "Paused")}
            </h4>
            <span className="muted small">
              {new Intl.DateTimeFormat(t("zh-CN", "en-US"), {
                dateStyle: "medium",
                timeStyle: "short",
              }).format(new Date(scan.created_at * 1000))}
            </span>
          </div>
          <progress value={scan.processed} max={Math.max(scan.archives, 1)} />
          <dl className="scan-stats">
            <div>
              <dt>{t("发现书籍文件", "Archives found")}</dt>
              <dd>{scan.archives}</dd>
            </div>
            <div>
              <dt>{t("已处理书籍文件", "Archives processed")}</dt>
              <dd>{scan.processed}</dd>
            </div>
            <div>
              <dt>{t("新登记（有元数据）", "New with metadata")}</dt>
              <dd>{scan.with_metadata}</dd>
            </div>
            <div>
              <dt>{t("未变 / 重复", "Unchanged / duplicate")}</dt>
              <dd>
                {scan.counts.unchanged} / {scan.counts.duplicate}
              </dd>
            </div>
            <div>
              <dt>{t("需处理项目", "Needs attention")}</dt>
              <dd>{scan.counts.error + scan.without_metadata}</dd>
            </div>
            <div>
              <dt>{t("待处理", "Pending")}</dt>
              <dd>{scan.counts.pending}</dd>
            </div>
            <div>
              <dt>{t("已跳过", "Skipped")}</dt>
              <dd>{scan.ignored}</dd>
            </div>
          </dl>
          <div className="section-head">
            <h4>{t("扫描结果", "Scan results")}</h4>
            <label className="checkbox">
              <input
                type="checkbox"
                checked={failedOnly}
                onChange={(event) => {
                  filterChanged.current = true;
                  setFailedOnly(event.target.checked);
                }}
              />
              {t("只看失败", "Failed only")}
            </label>
          </div>
          <details className="scan-item-list">
            <summary>
              {t(
                `查看 ${scan.items.length} 条结果`,
                `View ${scan.items.length} results`,
              )}
            </summary>
            <ul className="scan-items">
              {scan.items.map((item) => (
                <li key={item.id}>
                  <div>
                    <strong>
                      {item.path || t("书库根目录", "Library root")}
                    </strong>
                    {item.error_code && (
                      <p className="error-text small">{reason(item)}</p>
                    )}
                    {item.warnings.some(
                      (warning) =>
                        warning.startsWith("eze_") ||
                        warning.startsWith("native_") ||
                        warning === "multiple_eze_sidecars",
                    ) && (
                      <p className="muted small">
                        {t(
                          "随书信息需要检查，书籍仍可阅读。",
                          "Sidecar metadata needs attention; the book is still readable.",
                        )}
                      </p>
                    )}
                    {item.warnings.includes(
                      "existing_book_metadata_preserved",
                    ) && (
                      <p className="muted small">
                        {t(
                          "已有元数据保持不变，未采用本次随书信息文件中的元数据。",
                          "Existing book metadata was preserved; this sidecar was not applied automatically.",
                        )}
                      </p>
                    )}
                    {item.warnings.includes("changed_file_set_aside") && (
                      <p className="muted small">
                        {t(
                          "书库中的文件曾被改动，已用原文件修复；改动过的文件改名为 .changed 保留在旁边。",
                          "The library file had been modified and was repaired from this original; the modified file was kept beside it with a .changed suffix.",
                        )}
                      </p>
                    )}
                    {(item.warnings.includes("source_cleanup_failed") ||
                      item.warnings.includes(
                        "source_cleanup_permission_denied",
                      )) && (
                      <span>
                        {t(
                          "已导入；原件未能删除",
                          "Imported; the source could not be removed",
                        )}
                      </span>
                    )}
                    {item.warnings.includes("source_retained_for_review") && (
                      <Pill>{t("源文件已保留", "Source files retained")}</Pill>
                    )}
                  </div>
                  <span
                    className={`scan-state ${item.state === "error" ? "error-text" : "muted"}`}
                  >
                    {labels[item.state]}
                  </span>
                  {item.book_id && (
                    <details
                      onToggle={(event) => {
                        if (!event.currentTarget.open || item.previous_books)
                          return;
                        void request<{ previous_books: string[] }>(
                          `/api/scans/${scan.id}/items/${item.id}`,
                        )
                          .then((value) =>
                            setScan(
                              (old) =>
                                old && {
                                  ...old,
                                  items: old.items.map((entry) =>
                                    entry.id === item.id
                                      ? {
                                          ...entry,
                                          previous_books: value.previous_books,
                                        }
                                      : entry,
                                  ),
                                },
                            ),
                          )
                          .catch(action.setError);
                      }}
                    >
                      <summary>{t("版本关系", "Related editions")}</summary>
                      {item.previous_books?.map((id) => (
                        <RouteLink
                          key={id}
                          route={`book/${id}`}
                          navigate={navigate}
                        >
                          {t("查看旧版", "View previous edition")}
                        </RouteLink>
                      ))}
                    </details>
                  )}
                  {item.book_id && (
                    <RouteLink
                      className="subtle"
                      route={`book/${item.book_id}`}
                      navigate={navigate}
                    >
                      {t("查看书籍", "View book")}
                    </RouteLink>
                  )}
                </li>
              ))}
            </ul>
          </details>
          {scan.after !== null && (
            <button
              disabled={busy}
              onClick={() =>
                void act(async (signal) => {
                  const data = await request<ScanRun>(
                    `/api/scans/${scan.id}?after=${encodeURIComponent(scan.after!)}&state=${failedOnly ? "error" : ""}`,
                    { signal },
                  );
                  if (!signal.aborted)
                    setScan({ ...data, items: [...scan.items, ...data.items] });
                })
              }
            >
              {t("载入更多结果", "Load more results")}
            </button>
          )}
        </div>
      )}
    </section>
  );
}
