import { cooldownNotices, downloadStates, retryNotice } from "./downloadState";
import { useState } from "react";
import { Popover } from "radix-ui";
import { ListTodo, LoaderCircle } from "lucide-react";
import { errorMessage, request } from "./api";
import { usePolling } from "./polling";
import { Pill } from "./Pills";
import type { Translate } from "./types";
type Job = {
  id: string;
  state: string;
  input_url: string;
  received: number;
  total: number | null;
  title: string | null;
  retry_at?: number;
  attempts?: number;
};
type TaskSummary = {
  downloads: {
    running: number;
    waiting: number;
    completed: number;
    total: number;
    items: Job[];
  };
  scan: { state: string; processed: number; archives: number } | null;
  source_checks: Record<string, number>;
  cooldowns?: Record<string, number>;
};
export function TaskQueue({
  t,
  navigate,
}: {
  t: Translate;
  navigate: (route: string) => void;
}) {
  const [open, setOpen] = useState(false),
    [data, setData] = useState<TaskSummary | null>(null);
  const busy =
    !!data &&
    data.downloads.running +
      data.downloads.waiting +
      (data.scan?.state === "active" ? 1 : 0) >
      0;
  // 5 s while there is work or the popover is open, 30 s when idle; reopening refreshes at once.
  const { error } = usePolling(
    async (signal) => {
      const value = await request<TaskSummary>("/api/tasks", { signal });
      if (!signal.aborted) setData(value);
    },
    open || busy ? 5000 : 30_000,
    { key: open },
  );
  const active =
    (data?.downloads.running ?? 0) +
    (data?.downloads.waiting ?? 0) +
    (data?.scan?.state === "active" ? 1 : 0) +
    (data?.source_checks.queued ?? 0) +
    (data?.source_checks.running ?? 0);
  const go = (route: string) => {
    setOpen(false);
    navigate(route);
  };
  return (
    <Popover.Root open={open} onOpenChange={setOpen}>
      <Popover.Trigger
        className="icon-button task-trigger"
        aria-label={t("任务队列", "Task queue")}
      >
        {active ? (
          <LoaderCircle size={18} className="spinner" />
        ) : (
          <ListTodo size={18} />
        )}
        {active > 0 && <span>{active}</span>}
      </Popover.Trigger>
      <Popover.Portal>
        <Popover.Content
          className="task-popover glass"
          sideOffset={10}
          collisionPadding={12}
          align="end"
        >
          <header>
            <strong>{t("任务队列", "Task queue")}</strong>
            <button className="text-button" onClick={() => go("downloads")}>
              {t("查看下载", "View downloads")}
            </button>
          </header>
          {!!error && <p role="alert">{errorMessage(error, t)}</p>}
          {data && (
            <>
              {cooldownNotices(data.cooldowns, t).map((text) => (
                <p key={text} className="small" role="status">
                  {text}
                </p>
              ))}
              <div className="tags">
                <Pill>
                  {t(
                    `进行中 ${data.downloads.running}`,
                    `${data.downloads.running} running`,
                  )}
                </Pill>
                <Pill>
                  {t(
                    `等待 ${data.downloads.waiting}`,
                    `${data.downloads.waiting} waiting`,
                  )}
                </Pill>
                <Pill>
                  {t(
                    `已完成 ${data.downloads.completed}/${data.downloads.total}`,
                    `${data.downloads.completed}/${data.downloads.total} completed`,
                  )}
                </Pill>
              </div>
              {data.downloads.items.map((job) => (
                <button
                  className="task-row"
                  key={job.id}
                  onClick={() => go("downloads")}
                >
                  <strong>{job.title || job.input_url}</strong>
                  <small>
                    {retryNotice(job, t) ??
                      t(
                        ...(downloadStates[job.state] ?? [
                          "处理中",
                          "Processing",
                        ]),
                      )}
                  </small>
                  {job.total !== null && job.state === "downloading" && (
                    <progress value={job.received} max={job.total} />
                  )}
                </button>
              ))}
              {data.scan?.state === "active" && (
                <button className="task-row" onClick={() => go("settings")}>
                  <strong>{t("扫描导入", "Scan import")}</strong>
                  <span>
                    {data.scan.processed}/{data.scan.archives}
                  </span>
                </button>
              )}
              {(data.source_checks.queued ?? 0) +
                (data.source_checks.running ?? 0) >
                0 && (
                <Pill>
                  {t("来源更新", "Source updates")}{" "}
                  {(data.source_checks.queued ?? 0) +
                    (data.source_checks.running ?? 0)}
                </Pill>
              )}
              {!active && !data.downloads.items.length && (
                <p className="muted">
                  {t("没有等待中的任务", "No pending tasks")}
                </p>
              )}
            </>
          )}
        </Popover.Content>
      </Popover.Portal>
    </Popover.Root>
  );
}
