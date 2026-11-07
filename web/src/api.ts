import { sourceErrors } from "./sourceErrors";
import type { Translate } from "./types";

export class UserError extends Error {}

/** A non-2xx response. `code` and `detail` come from the JSON body `{error, message}` when present. */
export class ApiError extends Error {
  readonly code: string | null;
  readonly detail: string | null;
  constructor(
    public status: number,
    code: string | null = null,
    detail: string | null = null,
  ) {
    super(detail || `Request failed (${status})`);
    this.code = code;
    this.detail = detail;
  }
}

export class PageLimitError extends Error {
  constructor(public bytes: number) {
    super("Page response exceeds the browser limit");
  }
}

async function failure(response: Response) {
  const body: unknown = await response.json().catch(() => null);
  const field = (name: string) => {
    const value = (body as Record<string, unknown> | null)?.[name];
    return typeof value === "string" && value ? value : null;
  };
  return new ApiError(response.status, field("error"), field("message"));
}

export async function request<T>(
  path: string,
  options: RequestInit = {},
): Promise<T> {
  const controller = new AbortController();
  const cancel = () => controller.abort(options.signal?.reason);
  if (options.signal?.aborted) cancel();
  else options.signal?.addEventListener("abort", cancel, { once: true });
  const timer = setTimeout(
    () =>
      controller.abort(new DOMException("Request timed out", "TimeoutError")),
    path.startsWith("/api/import") ? 30 * 60_000 : 60_000,
  );
  try {
    const response = await fetch(path, {
      ...options,
      signal: controller.signal,
      credentials: "same-origin",
      headers: {
        "X-MangaCouch-Request": "1",
        ...(typeof options.body === "string"
          ? { "Content-Type": "application/json" }
          : {}),
        ...options.headers,
      },
    });
    if (!response.ok) {
      if (response.status === 401 && path !== "/api/auth/login")
        window.dispatchEvent(new Event("mc-locked"));
      throw await failure(response);
    }
    return (await response.json()) as T;
  } finally {
    clearTimeout(timer);
    options.signal?.removeEventListener("abort", cancel);
  }
}
export const post = <T>(path: string, data?: unknown, signal?: AbortSignal) =>
  request<T>(path, {
    method: "POST",
    signal,
    ...(data === undefined ? {} : { body: JSON.stringify(data) }),
  });
export const put = <T>(path: string, data: unknown, signal?: AbortSignal) =>
  request<T>(path, { method: "PUT", signal, body: JSON.stringify(data) });
export const pagePath = (bookId: string, page: number, sha256?: string) =>
  `/api/books/${encodeURIComponent(bookId)}/pages/${page}${sha256 ? `?v=${encodeURIComponent(sha256.slice(0, 16))}` : ""}`;
export const aborted = (error: unknown) =>
  error instanceof DOMException && error.name === "AbortError";

const conflict: [string, string] = [
  "另一设备已修改这些内容，请重新载入或明确重试当前修改。",
  "Another device changed this item. Reload or retry your changes.",
];
const unavailable: [string, string] = [
  "书库暂不可用，请检查存储位置后重试。",
  "Library storage is unavailable. Check its location and retry.",
];
const tooMany: [string, string] = [
  "尝试次数过多，请稍后再试。",
  "Too many attempts. Try again later.",
];
/** Stable server error codes (contract: `{"error": code, "message": text}`). */
const codes: Record<string, [string, string]> = {
  ...sourceErrors,
  invalid_credentials: [
    "口令不正确或会话已失效，请重新登录。",
    "Incorrect passcode or expired session. Please sign in.",
  ],
  key_scope: [
    "此 API key 无权执行该操作。",
    "This API key is not allowed to do that.",
  ],
  rate_limited: tooMany,
  revision_conflict: conflict,
  refresh_required: conflict,
  not_found: [
    "内容不存在或已移除，请刷新后重试。",
    "This item is missing or was removed. Refresh and try again.",
  ],
  invalid_input: [
    "输入未通过检查，请检查后重试。",
    "The input did not pass validation. Check it and try again.",
  ],
  root_unavailable: unavailable,
  storage_unavailable: unavailable,
  insufficient_storage: [
    "服务器磁盘空间不足，请腾出空间后重试。",
    "The server is low on disk space. Free some space and try again.",
  ],
  internal_error: [
    "服务器内部错误，请查看服务器日志。",
    "Internal server error. Check the server log.",
  ],
  bad_request: [
    "请求无效，请刷新页面后重试。",
    "The request was rejected. Reload and try again.",
  ],
  forbidden: ["当前请求没有操作权限。", "This request is not permitted."],
  body_too_large: ["文件超过上传上限。", "This file exceeds the upload limit."],
  unavailable: unavailable,
  // Import and scan reasons (also stored as scan item error codes).
  archive_too_large: [
    "本子超过导入大小上限。",
    "The archive exceeds the import size limit.",
  ],
  unsupported_format: [
    "只支持 ZIP、CBZ 和 PDF。",
    "Only ZIP, CBZ and PDF files are supported.",
  ],
  invalid_archive: [
    "本子文件损坏或格式不支持。",
    "The archive is damaged or unsupported.",
  ],
  content_changed: [
    "文件或随书信息发生变化，请检查后重试。",
    "The archive or sidecar changed. Check it before retrying.",
  ],
  source_missing: [
    "文件已移动或暂时不可用。",
    "The file moved or is unavailable.",
  ],
  directory_too_large: [
    "单目录超过 100,000 项，未展开；拆分目录后重扫。",
    "Folder exceeds 100,000 entries; split it and scan again.",
  ],
  storage_error: [
    "读取失败，请检查存储和文件。",
    "Read failed. Check the storage and file.",
  ],
  database_error: [
    "数据库写入失败，请查看服务器日志。",
    "The database write failed. Check the server log.",
  ],
};
/** Localized text for a stable error code, or null when the code is unknown. */
export const codeMessage = (code: string | null | undefined, t: Translate) =>
  code && codes[code] ? t(...codes[code]) : null;
const statuses: Record<number, [string, string]> = {
  400: [
    "请求无效，请刷新页面后重试。",
    "The request was rejected. Reload and try again.",
  ],
  401: codes.invalid_credentials!,
  403: ["当前请求没有操作权限。", "This request is not permitted."],
  404: codes.not_found!,
  409: conflict,
  413: codes.body_too_large!,
  422: codes.invalid_input!,
  429: tooMany,
  503: unavailable,
  507: codes.insufficient_storage!,
};

/**
 * The one user-facing text for any failure: a known error code, then the server's own
 * message, then the HTTP status. Only failures without a response read as a connection problem.
 */
export function errorMessage(error: unknown, t: Translate): string {
  if (error instanceof UserError) return error.message;
  if (error instanceof DOMException && error.name === "TimeoutError")
    return t("请求超时，请稍后重试。", "Request timed out. Try again.");
  if (error instanceof PageLimitError)
    return t(
      `单页响应超过浏览器 ${Math.round(error.bytes / 1024 ** 2)} MiB 上限。`,
      `This page exceeds the browser's ${Math.round(error.bytes / 1024 ** 2)} MiB response limit.`,
    );
  if (error instanceof ApiError) {
    const known = codeMessage(error.code, t);
    if (known) return known;
    if (error.detail) return error.detail;
    const status = statuses[error.status];
    if (status) return t(...status);
    return t(
      `请求失败（${error.status}），请稍后重试。`,
      `Request failed (${error.status}). Try again later.`,
    );
  }
  return t(
    "连接失败，修改可能尚未保存，请重试。",
    "Connection failed. Changes may not be saved. Please retry.",
  );
}
/** @deprecated alias kept for files that still import `message`; use `errorMessage`. */
export const message = errorMessage;
