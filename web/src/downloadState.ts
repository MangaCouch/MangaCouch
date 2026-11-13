import { haltEnd } from "./sourceErrors";
import type { Translate } from "./types";

export const downloadStates: Record<string, [string, string]> = {
  queued: ["等待处理", "Queued"],
  inspecting: ["获取来源", "Fetching source"],
  needs_approval: ["等待下载确认", "Confirm download"],
  submitting: ["提交下载", "Requesting archive"],
  downloading: ["下载中", "Downloading"],
  importing: ["正在入库", "Importing"],
  done: ["已入库", "In library"],
  paused: ["已暂停", "Paused"],
  pausing: ["正在暂停", "Pausing"],
  cancelled: ["已取消", "Cancelled"],
  cancelling: ["正在取消", "Cancelling"],
  error: ["需要重试", "Retry needed"],
  verification_required: ["需要处理来源登录", "Source sign-in needed"],
};

export function downloadLabel(
  job: { state: string; error_code?: string | null },
  t: Translate,
) {
  return job.state === "queued" && job.error_code === "insufficient_gp"
    ? t("GP 不足，等待中", "Waiting for GP")
    : t(...(downloadStates[job.state] ?? [job.state, job.state]));
}

/**
 * Jobs the worker is moving forward right now (a retry wait counts: it resumes on its own),
 * including ones still winding down after a pause or cancel request.
 */
export const activeStates = [
  "queued",
  "inspecting",
  "submitting",
  "downloading",
  "importing",
  "pausing",
  "cancelling",
];
/** Finished jobs; nothing more can happen to them except a new request. */
export const terminalStates = ["done", "cancelled"];
export const resumableStates = ["paused", "error", "verification_required"];
/** Jobs whose record can be cleared at once: finished ones and failed ones. */
export const clearable = (state: string) =>
  terminalStates.includes(state) || state === "error";
export const cancellable = (state: string) =>
  !terminalStates.includes(state) && state !== "cancelling";
/** Running jobs that a pause request still changes. */
export const pausable = (state: string) =>
  activeStates.includes(state) && state !== "pausing" && state !== "cancelling";

const siteNames: Record<string, string> = {
  ehentai: "E-Hentai",
  exhentai: "ExHentai",
};

/** The non-empty, trimmed lines of a pasted list of links. */
export const lines = (text: string) =>
  text
    .split(/\r?\n/)
    .map((line) => line.trim())
    .filter(Boolean);

/**
 * `text` without the submitted lines the server took (each as often as it took it), so links
 * pasted while the request ran, and lines it refused, stay in the box.
 */
export function withoutLines(text: string, handled: string[]) {
  const left = new Map<string, number>();
  for (const line of handled) left.set(line, (left.get(line) ?? 0) + 1);
  if (!left.size) return text;
  return lines(text)
    .filter((line) => {
      const count = left.get(line) ?? 0;
      if (count) left.set(line, count - 1);
      return !count;
    })
    .join("\n");
}

/**
 * Waits the server does not count as failed attempts (downloads.py `_finish`): the attempt
 * number would stand still through them, so the notice leaves it out.
 */
const uncountedWaits = new Set([
  "archive_preparing",
  "database_busy",
  "root_unavailable",
  "insufficient_storage",
  "insufficient_gp",
  "source_rate_limited",
]);

/** A transient failure retried automatically stays queued with a future `retry_at`. */
export function retryNotice(
  job: {
    state: string;
    retry_at?: number;
    attempts?: number;
    error_code?: string | null;
  },
  t: Translate,
  now = Date.now() / 1000,
) {
  if (job.state === "queued" && job.error_code === "insufficient_gp")
    return t(
      "GP 不足，等待中；已保留下载确认，逐步延长间隔检查余额，充值后自动继续。",
      "Waiting for GP; your approval is saved. Balance checks gradually become less frequent and downloads resume after topping up.",
    );
  if (job.state !== "queued" || !job.retry_at || job.retry_at <= now)
    return null;
  const at = haltEnd(job.retry_at, t, now * 1000),
    attempt = (job.attempts ?? 0) + 1;
  return job.attempts && !uncountedWaits.has(job.error_code ?? "")
    ? t(
        `将于 ${at} 自动重试（第 ${attempt} 次）`,
        `Retrying at ${at}, attempt ${attempt}`,
      )
    : t(`将于 ${at} 自动重试`, `Retrying automatically at ${at}`);
}

/**
 * One line per source halt after a rate limit (a ban, a "too fast" warning or a 429). Sites
 * halted together (E-Hentai and ExHentai share one) share a line.
 */
export function cooldownNotices(
  cooldowns: Record<string, number> | null | undefined,
  t: Translate,
  now = Date.now() / 1000,
) {
  const halts = new Map<number, string[]>();
  for (const [site, until] of Object.entries(cooldowns ?? {}))
    if (until > now)
      halts.set(until, [...(halts.get(until) ?? []), siteNames[site] ?? site]);
  return [...halts].map(([until, names]) => {
    const name = names.join(" / "),
      at = haltEnd(until, t, now * 1000);
    return t(
      `${name} 限流，请求暂停至 ${at}；下载和来源检查届时自动继续。`,
      `${name} rate-limited until ${at}; downloads and source checks resume automatically.`,
    );
  });
}
