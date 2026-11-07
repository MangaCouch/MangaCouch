import type { Translate } from "./types";

export const downloadStates: Record<string, [string, string]> = {
  queued: ["等待处理", "Queued"],
  inspecting: ["获取来源", "Fetching source"],
  needs_choice: ["选择版本", "Choose edition"],
  needs_approval: ["等待下载确认", "Confirm download"],
  submitting: ["提交下载", "Requesting archive"],
  downloading: ["下载中", "Downloading"],
  importing: ["正在入库", "Importing"],
  done: ["已入库", "In library"],
  paused: ["已暂停", "Paused"],
  pausing: ["正在暂停", "Pausing"],
  cancelled: ["已取消", "Cancelled"],
  cancelling: ["正在取消", "Cancelling"],
  skipped: ["已取消", "Cancelled"],
  error: ["需要重试", "Retry needed"],
  verification_required: ["需要更新 Cookie", "Update cookies"],
  needs_attention: ["等待确认远端结果", "Check remote result"],
};

/** Jobs the worker is moving forward right now (a retry wait counts: it resumes on its own). */
export const activeStates = [
  "queued",
  "inspecting",
  "submitting",
  "downloading",
  "importing",
];
/** Finished jobs; nothing more can happen to them except a new request. */
export const terminalStates = ["done", "cancelled", "skipped"];
export const resumableStates = [
  "paused",
  "error",
  "verification_required",
  "needs_attention",
  "cancelled",
  "skipped",
];
export const cancellable = (state: string) =>
  !terminalStates.includes(state) && state !== "cancelling";

export const siteNames: Record<string, string> = {
  ehentai: "E-Hentai",
  exhentai: "ExHentai",
  nhentai: "nHentai",
};
const clock = (seconds: number, t: Translate) =>
  new Date(seconds * 1000).toLocaleTimeString(t("zh-CN", "en-US"), {
    hour: "2-digit",
    minute: "2-digit",
  });

/** Contract 7: a transient failure retried automatically stays queued with a future `retry_at`. */
export function retryNotice(
  job: { state: string; retry_at?: number; attempts?: number },
  t: Translate,
  now = Date.now() / 1000,
) {
  if (job.state !== "queued" || !job.retry_at || job.retry_at <= now)
    return null;
  const at = clock(job.retry_at, t),
    attempt = (job.attempts ?? 0) + 1;
  return t(
    `将于 ${at} 自动重试（第 ${attempt} 次）`,
    `Retrying at ${at}, attempt ${attempt}`,
  );
}

/** Contract 8: one line per source that is cooling down after a rate limit. */
export function cooldownNotices(
  cooldowns: Record<string, number> | null | undefined,
  t: Translate,
  now = Date.now() / 1000,
) {
  return Object.entries(cooldowns ?? {})
    .filter(([, until]) => until > now)
    .map(([site, until]) => {
      const name = siteNames[site] ?? site,
        at = clock(until, t);
      return t(
        `${name} 限流至 ${at}，队列届时自动继续。`,
        `${name} rate-limited until ${at}; queue resumes automatically.`,
      );
    });
}
