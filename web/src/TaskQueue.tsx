import { sourceMessage } from "./sourceErrors";
import { RouteLink } from "./navigation";
import { cooldownNotices, downloadLabel, retryNotice } from "./downloadState";
import {
  createContext,
  useContext,
  useEffect,
  useState,
  type ReactNode,
} from "react";
import { Popover } from "radix-ui";
import { CircleAlert, ListTodo, LoaderCircle } from "lucide-react";
import { errorMessage, request } from "./api";
import { usePolling } from "./polling";
import { Pill } from "./Pills";
import type { WarmStatus } from "./Cache";
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
  error_code?: string | null;
};
type TaskSummary = {
  downloads: {
    blocked?: string | null;
    storage_wait_until?: number;
    running: number;
    waiting: number;
    needs_you: number;
    completed: number;
    total: number;
    items: Job[];
  };
  scan: { state: string; processed: number; archives: number } | null;
  source_checks: Record<string, number>;
  cooldowns?: Record<string, number>;
  covers?: WarmStatus;
  thumbnails?: WarmStatus;
};
/** Everything the server is working on or waiting to do; the header badge shows it. */
const activeCount = (data: TaskSummary | null) =>
  !data
    ? 0
    : (data.downloads.blocked
        ? 0
        : data.downloads.running + data.downloads.waiting) +
      (data.scan?.state === "active" ? 1 : 0) +
      (data.downloads.blocked
        ? 0
        : (data.source_checks.queued ?? 0) +
          (data.source_checks.running ?? 0)) +
      (data.covers?.state === "active" ? 1 : 0) +
      (data.thumbnails?.state === "active" ? 1 : 0);
const TaskContext = createContext<{
  data: TaskSummary | null;
  error: unknown;
  refresh: () => Promise<void>;
}>({ data: null, error: null, refresh: async () => {} });
export const useTaskSummary = () => useContext(TaskContext);
export function TaskProvider({
  enabled,
  children,
}: {
  enabled: boolean;
  children: ReactNode;
}) {
  const [data, setData] = useState<TaskSummary | null>(null);
  const { error, refresh } = usePolling(
    async (signal) => {
      const value = await request<TaskSummary>("/api/tasks", { signal });
      if (!signal.aborted) setData(value);
    },
    activeCount(data) ? 5000 : 30000,
    { enabled },
  );
  return (
    <TaskContext.Provider
      value={{ data: enabled ? data : null, error, refresh }}
    >
      {children}
    </TaskContext.Provider>
  );
}
export function TaskQueue({
  t,
  navigate,
}: {
  t: Translate;
  navigate: (route: string) => void;
}) {
  const [open, setOpen] = useState(false);
  const { data, error, refresh } = useTaskSummary();
  const active = activeCount(data);
  useEffect(() => {
    if (open) void refresh();
  }, [open, refresh]);
  const go = (route: string) => {
    setOpen(false);
    navigate(route);
  };
  return (
    <Popover.Root open={open} onOpenChange={setOpen}>
      {!!data?.downloads.needs_you && (
        <RouteLink
          className="icon-button attention-trigger"
          aria-label={t(
            `待处理的下载：${data.downloads.needs_you}`,
            `Downloads waiting for you: ${data.downloads.needs_you}`,
          )}
          title={t("待处理的下载", "Downloads waiting for you")}
          route={"downloads"}
          navigate={go}
        >
          <CircleAlert size={18} />
          <span className="attention-count">{data.downloads.needs_you}</span>
        </RouteLink>
      )}
      <Popover.Trigger
        className="icon-button head-action task-trigger"
        aria-label={t("任务队列", "Task queue")}
        title={t(
          "任务队列：下载、扫描与来源检查",
          "Task queue: downloads, scans and source checks",
        )}
      >
        {active ? (
          <LoaderCircle size={18} className="spinner" />
        ) : (
          <ListTodo size={18} />
        )}
        <span className="head-label">{t("任务队列", "Task queue")}</span>
        {active > 0 && <span className="task-count">{active}</span>}
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
            <RouteLink
              className="text-button"
              route={"downloads"}
              navigate={go}
            >
              {t("查看下载", "View downloads")}
            </RouteLink>
          </header>
          {!!error && (
            <p role="alert" className="error-text">
              {errorMessage(error, t)}
            </p>
          )}
          {data && (
            <>
              {data.downloads.blocked && (
                <p role="alert" className="error-text">
                  {sourceMessage(data.downloads.blocked, t)}{" "}
                  <RouteLink route="settings" navigate={go}>
                    {t("打开设置", "Open settings")}
                  </RouteLink>
                </p>
              )}
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
                <RouteLink
                  className="task-row"
                  key={job.id}
                  route={`downloads/${job.id}`}
                  navigate={go}
                >
                  <strong>{job.title || job.input_url}</strong>
                  <small>{retryNotice(job, t) ?? downloadLabel(job, t)}</small>
                  {job.total !== null && job.state === "downloading" && (
                    <progress value={job.received} max={job.total} />
                  )}
                </RouteLink>
              ))}
              {data.scan?.state === "active" && (
                <RouteLink
                  className="task-row"
                  route={"settings/scan"}
                  navigate={go}
                >
                  <strong>{t("扫描导入", "Scan import")}</strong>
                  <span>
                    {data.scan.processed}/{data.scan.archives}
                  </span>
                </RouteLink>
              )}
              {(
                [
                  ["covers", t("生成封面", "Rendering covers")],
                  ["thumbnails", t("生成缩略图", "Rendering thumbnails")],
                ] as const
              ).map(([kind, label]) => {
                const warm = data[kind];
                return (
                  warm?.state === "active" && (
                    <RouteLink
                      key={kind}
                      className="task-row"
                      route={"settings/cache"}
                      navigate={go}
                    >
                      <strong>{label}</strong>
                      <span>
                        {warm.done}/{warm.total}
                      </span>
                      <progress
                        value={warm.done}
                        max={Math.max(1, warm.total)}
                      />
                    </RouteLink>
                  )
                );
              })}
              {(data.source_checks.queued ?? 0) +
                (data.source_checks.running ?? 0) >
                0 && (
                <RouteLink
                  className="task-row"
                  route={"settings/source-checks"}
                  navigate={go}
                >
                  {t("来源核对", "Source review")}{" "}
                  {(data.source_checks.queued ?? 0) +
                    (data.source_checks.running ?? 0)}
                </RouteLink>
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
