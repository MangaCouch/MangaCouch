import { useEffect, useRef, useState } from "react";
import { FolderSearch, Pause } from "lucide-react";
import { ApiError, aborted, codeMessage, errorMessage, request } from "./api";
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
  after: number | null;
}

export function Scan({
  t,
  navigate,
}: {
  t: Translate;
  navigate: (route: string) => void;
}) {
  const [scan, setScan] = useState<ScanRun | null>(null),
    [root, setRoot] = useState("");
  const [keepSource, setKeepSource] = useState(false);
  const [busy, setBusy] = useState(false),
    [error, setError] = useState<unknown>(null),
    [notice, setNotice] = useState("");
  const [loaded, setLoaded] = useState(false);
  const command = useRef<AbortController | null>(null);
  useEffect(() => () => command.current?.abort(), []);
  // Follows a running scan closely; otherwise only checks now and then.
  const { error: loadError } = usePolling(
    async (signal) => {
      const data = await request<{ scan: ScanRun | null; root: string }>(
        "/api/scans",
        { signal },
      );
      if (signal.aborted) return;
      setScan((old) => {
        if (!old || !data.scan || old.id !== data.scan.id) return data.scan;
        const updated = new Map(old.items.map((item) => [item.id, item]));
        data.scan.items.forEach((item) => updated.set(item.id, item));
        return {
          ...data.scan,
          items: [...updated.values()].sort((a, b) => a.id - b.id),
          after:
            old.items.length > data.scan.items.length
              ? old.after
              : data.scan.after,
        };
      });
      setRoot(data.root);
      setLoaded(true);
      if (data.scan && data.scan.state !== "done")
        setKeepSource(data.scan.keep_source);
    },
    scan?.state === "active" ? 1500 : 15_000,
    { enabled: !busy },
  );
  const loading = !loaded && !loadError;
  async function act(action: (signal: AbortSignal) => Promise<void>) {
    if (busy) return;
    const controller = new AbortController();
    command.current = controller;
    setBusy(true);
    setError(null);
    setNotice("");
    try {
      await action(controller.signal);
    } catch (error) {
      if (!aborted(error)) setError(error);
    } finally {
      setBusy(false);
    }
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
          {error instanceof ApiError && error.status === 409
            ? t("扫描正在执行。", "A scan is already running.")
            : errorMessage(error ?? loadError, t)}
        </p>
      )}
      {notice && (
        <p className="notice" role="status">
          {notice}
        </p>
      )}
      {["RootUnavailable", "storage_unavailable", "root_unavailable"].includes(
        scan?.error_code ?? "",
      ) && (
        <p className="notice error" role="alert">
          {t(
            "书库目录不可用，扫描已暂停，已有记录保留。修复挂载或路径后再继续。",
            "Library storage is unavailable. Scanning is paused and existing records are kept. Repair the mount or path before continuing.",
          )}
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
      </div>
      {loading && (
        <p role="status">{t("读取扫描记录…", "Loading scan records…")}</p>
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
              <dt>{t("发现本子", "Archives found")}</dt>
              <dd>{scan.archives}</dd>
            </div>
            <div>
              <dt>{t("已处理本子", "Archives processed")}</dt>
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
          </dl>
          <p className="muted small">
            {t(
              `发现数量随目录展开增加。待处理 ${scan.counts.pending} 个文件或目录；已跳过 ${scan.ignored} 个非本子文件、内部文件或链接。`,
              `Discovery grows as folders are listed. ${scan.counts.pending} files or folders pending; ${scan.ignored} non-archives, internal entries or links skipped.`,
            )}
          </p>
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
                    <button
                      className="subtle"
                      onClick={() => navigate(`book/${item.book_id}`)}
                    >
                      {t("查看书籍", "View book")}
                    </button>
                  )}
                </li>
              ))}
            </ul>
          </details>
          {scan.after !== null && (
            <button
              disabled={busy || scan?.state === "active"}
              onClick={() =>
                void act(async (signal) => {
                  const data = await request<ScanRun>(
                    `/api/scans/${scan.id}?after=${scan.after}`,
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
          {scan.state === "done" && scan.counts.error > 0 && (
            <p className="muted small">
              {t(
                "失败项不会隐藏。修复后再次开始扫描，已登记且未变的文件会跳过。",
                "Failed items stay listed. Fix them and start another scan; indexed, unchanged files will be skipped.",
              )}
            </p>
          )}
        </div>
      )}
    </section>
  );
}
