import { haltMessage, sourceErrors } from "./sourceErrors";
import type { Translate } from "./types";

export class UserError extends Error {}

/**
 * A non-2xx response. `code`, `detail` and `retryAt` come from the JSON body
 * `{error, message, retry_at}` when present; `retryAt` (epoch seconds) ends a source halt.
 */
export class ApiError extends Error {
  readonly code: string | null;
  readonly detail: string | null;
  readonly retryAt: number | null;
  constructor(
    public status: number,
    code: string | null = null,
    detail: string | null = null,
    retryAt: number | null = null,
    public body?: Record<string, unknown>,
  ) {
    super(detail || `Request failed (${status})`);
    this.code = code;
    this.detail = detail;
    this.retryAt = retryAt;
  }
}

export const uncertainWrite = (error: unknown) =>
  !(error instanceof ApiError) || error.status >= 500;

export class PageLimitError extends Error {
  constructor(public bytes: number) {
    super("Page response exceeds the browser limit");
  }
}

export async function failure(response: Response) {
  const body: unknown = await response.json().catch(() => null);
  const field = (name: string) => {
    const value = (body as Record<string, unknown> | null)?.[name];
    return typeof value === "string" && value ? value : null;
  };
  const retryAfter = Number(response.headers.get("Retry-After"));
  const retryAt =
    (body as Record<string, unknown> | null)?.retry_at ??
    (Number.isFinite(retryAfter) && retryAfter > 0
      ? Date.now() / 1000 + retryAfter
      : null);
  return new ApiError(
    response.status,
    field("error"),
    field("message"),
    typeof retryAt === "number" ? retryAt : null,
    body && typeof body === "object"
      ? (body as Record<string, unknown>)
      : undefined,
  );
}

/**
 * A signal that follows `source` and also aborts with a `TimeoutError` after `ms`.
 * Call `release()` once the work is over. Written by hand because
 * `AbortSignal.any` is missing before Safari/iOS 17.4.
 */
export function timeoutSignal(
  source: AbortSignal | null | undefined,
  ms: number,
) {
  const controller = new AbortController();
  const cancel = () => controller.abort(source?.reason);
  if (source?.aborted) cancel();
  else source?.addEventListener("abort", cancel, { once: true });
  const timer = setTimeout(
    () =>
      controller.abort(new DOMException("Request timed out", "TimeoutError")),
    ms,
  );
  return {
    signal: controller.signal,
    release() {
      clearTimeout(timer);
      source?.removeEventListener("abort", cancel);
    },
  };
}

export async function request<T>(
  path: string,
  options: RequestInit = {},
): Promise<T> {
  const timed = timeoutSignal(
    options.signal,
    // Imports and backups may take the server a while to process or build.
    path.startsWith("/api/import") || path === "/api/snapshots"
      ? 30 * 60_000
      : 60_000,
  );
  const signal = timed.signal;
  try {
    const send = () =>
      fetch(path, {
        ...options,
        signal,
        credentials: "same-origin",
        headers: {
          "X-MangaCouch-Request": "1",
          ...(typeof options.body === "string"
            ? { "Content-Type": "application/json" }
            : {}),
          ...options.headers,
        },
      });
    let response = await send();
    const retry = Number(response.headers.get("Retry-After"));
    if (
      (options.method ?? "GET").toUpperCase() === "GET" &&
      response.status === 503 &&
      response.headers.has("Retry-After") &&
      Number.isFinite(retry) &&
      retry >= 0 &&
      retry <= 5
    ) {
      await response.body?.cancel();
      await new Promise<void>((resolve, reject) => {
        const cancelWait = () => {
          clearTimeout(wait);
          reject(signal.reason);
        };
        const wait = setTimeout(() => {
          signal.removeEventListener("abort", cancelWait);
          resolve();
        }, retry * 1000);
        signal.addEventListener("abort", cancelWait, { once: true });
        if (signal.aborted) cancelWait();
      });
      response = await send();
    }
    if (!response.ok) {
      if (response.status === 401 && path !== "/api/auth/login")
        window.dispatchEvent(new Event("mc-locked"));
      throw await failure(response);
    }
    // 204 No Content acknowledges a change that has no resource to return.
    if (response.status === 204) return undefined as T;
    return (await response.json()) as T;
  } finally {
    timed.release();
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

/**
 * A per-device stamp makes every image load again after clearing this device's cache.
 * Per-book thumbnail refresh revisions come from the server as part of `v`.
 */
type ImageStamps = { epoch?: number };
const STAMPS = "mangacouch:image-stamps";
let stamps: ImageStamps | null = null;
function readStamps(): ImageStamps {
  if (stamps) return stamps;
  try {
    stamps = JSON.parse(localStorage.getItem(STAMPS) ?? "{}") as ImageStamps;
  } catch {
    stamps = {};
  }
  return stamps;
}
function writeStamps(next: ImageStamps) {
  stamps = next;
  try {
    localStorage.setItem(STAMPS, JSON.stringify(next));
  } catch {
    /* Storage may be unavailable; the stamps then last for this page load. */
  }
}
/** Makes this device fetch every image again (its old cache entries go unused). */
export function restampAllImages() {
  writeStamps({ epoch: Date.now() });
}
/** An image URL with its content version (`v`) and this device's stamps. */
export function imageUrl(path: string, version?: string) {
  const params = new URLSearchParams();
  if (version) params.set("v", version);
  const stamp = readStamps().epoch ?? 0;
  if (stamp) params.set("r", stamp.toString(36));
  const query = params.toString();
  return query ? `${path}?${query}` : path;
}
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
    "此 API 密钥无权执行该操作。",
    "This API key is not allowed to do that.",
  ],
  rate_limited: tooMany,
  login_rate_limited: ["登录尝试过多。", "Too many sign-in attempts."],
  login_busy: ["登录验证繁忙。", "Sign-in verification is busy."],
  cancelled: ["操作已取消。", "The operation was cancelled."],
  import_failed: [
    "导入失败，请检查文件后重试。",
    "Import failed. Check the file and retry.",
  ],
  revision_conflict: conflict,
  external_conflict: [
    "外部 sidecar 与书库记录不一致，请处理同步冲突。",
    "The external sidecar differs from this library. Resolve the sync conflict.",
  ],
  removal_scan_active: [
    "扫描仍在进行，请先暂停扫描再重新检查。",
    "A scan is still active. Pause it and check again.",
  ],
  removal_import_pending: [
    "该书仍有未完成的导入，请先处理导入任务。",
    "This book has an unfinished import. Resolve it first.",
  ],
  removal_archive_changed: [
    "文件内容已与这本书不一致（被替换或修改过），不会随记录删除。可仅移除记录，或放回原文件后重新检查。",
    "The file's content no longer matches this book (it was replaced or edited), so it is not deleted. Remove only the record, or put the original file back and check again.",
  ],
  removal_archive_unreadable: [
    "文件无法读取，请检查权限与存储连接；仍可仅移除记录。",
    "The file cannot be read. Check permissions and storage, or remove only the record.",
  ],
  removal_pending_deletion: [
    "请先停止待处理的文件删除，再恢复记录。",
    "Stop the pending file deletion before restoring records.",
  ],
  removal_files_unavailable: [
    "原文件缺失或内容已变化，请放回原文件后再恢复。",
    "The original files are missing or changed. Restore those files before retrying.",
  ],
  removal_already_restored: [
    "这些书已重新进入书库，请刷新列表。",
    "These books are already in the library. Refresh the list.",
  ],
  not_found: [
    "内容不存在或已移除，请刷新后重试。",
    "This item is missing or was removed. Refresh and try again.",
  ],
  invalid_proxy: [
    "代理地址应使用 http、https、socks5 或 socks5h，且不含路径或片段。",
    "Use an http, https, socks5 or socks5h proxy URL without a path or fragment.",
  ],
  invalid_credential: [
    "凭据不能只有空格，也不能包含换行或非 ASCII 字符，最多 16384 个字符。",
    "Credentials cannot be whitespace-only or contain line breaks or non-ASCII characters; the limit is 16384 characters.",
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
    "文件超过导入大小上限。",
    "The archive exceeds the import size limit.",
  ],
  unsupported_format: [
    "只支持 ZIP、CBZ 和 PDF。",
    "Only ZIP, CBZ and PDF files are supported.",
  ],
  invalid_archive: [
    "书籍文件损坏或格式不支持。",
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
  library_busy: [
    "书库正在处理其他操作，请稍后重试",
    "The library is busy with another operation; try again shortly",
  ],
  database_busy: [
    "书库正忙，请稍后重试。",
    "The library is busy. Try again in a moment.",
  ],
  // Sidecar (metadata file) sync failures recorded per book.
  sidecar_invalid: [
    "随书信息文件已损坏、无法读取；可以用书库记录覆盖它。",
    "The metadata file is damaged and cannot be read; it can be overwritten from the library record.",
  ],
  sidecar_changed: [
    "随书信息文件在保存期间被其他地方改动，稍后会重新同步。",
    "The metadata file changed elsewhere while saving; it will be synced again shortly.",
  ],
  archive_missing: [
    "找不到书籍文件，随书信息无法写入。",
    "The book file is missing, so its metadata file cannot be written.",
  ],
  archive_changed: [
    "书籍文件已被修改，请重新扫描后再同步。",
    "The book file changed on disk. Scan again before syncing.",
  ],
  archive_unreadable: [
    "书籍文件无法读取，请检查文件和存储权限。",
    "The book file cannot be read. Check the file and storage permissions.",
  ],
  storage_error: [
    "读取失败，请检查存储和文件。",
    "Read failed. Check the storage and file.",
  ],
  database_error: [
    "数据库写入失败，请查看服务器日志。",
    "The database write failed. Check the server log.",
  ],
  download_done: [
    "这次下载已入库，无需再操作。",
    "This download is already in the library.",
  ],
  download_active: [
    "只能清除已结束的下载。",
    "Only finished downloads can be cleared.",
  ],
  download_not_found: [
    "下载记录不存在或已清除，请刷新列表。",
    "This download no longer exists. Refresh the list.",
  ],
  // Request checks in front of every route (http.py).
  same_origin_required: [
    "服务器拒绝了这次修改：当前访问地址与服务器配置的地址（MANGACOUCH_ORIGIN / --origin）不一致。请改用配置的地址打开，或让管理员更正该设置。",
    "The server refused this change: the address you opened differs from the one it is configured for (MANGACOUCH_ORIGIN / --origin). Open the configured address, or ask the administrator to correct it.",
  ],
  cross_site_request: [
    "服务器拒绝了来自其他网站的请求。请直接打开书库地址；若经反向代理访问，请检查代理和 MANGACOUCH_ORIGIN 是否与访问地址一致。",
    "The server refused a request from another site. Open the library address directly; behind a reverse proxy, check that the proxy and MANGACOUCH_ORIGIN match the address you use.",
  ],
  invalid_host: [
    "服务器不接受这个访问地址。请检查 MANGACOUCH_ALLOWED_HOSTS / --allowed-host 设置。",
    "The server does not accept this address. Check MANGACOUCH_ALLOWED_HOSTS / --allowed-host.",
  ],
  read_only: [
    "此 API 密钥为只读，不能修改书库。",
    "This API key is read-only and cannot change the library.",
  ],
  body_timeout: [
    "请求内容上传超时，请检查网络后重试。",
    "The request took too long to arrive. Check the connection and retry.",
  ],
  request_timeout: [
    "上传停滞超时，请检查网络后重试。",
    "The upload stalled and timed out. Check the connection and retry.",
  ],
  method_not_allowed: [
    "服务器不支持此操作，请刷新页面后重试。",
    "The server does not support this operation. Reload and try again.",
  ],
  too_many_credentials: [
    "密钥数量已达上限，请先撤销不再使用的密钥。",
    "Too many keys. Revoke unused keys before creating more.",
  ],
  // Library, collections and review.
  book_not_found: [
    "这本书不存在或已被移除，请刷新后重试。",
    "This book is missing or was removed. Refresh and try again.",
  ],
  collection_not_found: [
    "合集不存在或已被删除，请刷新后重试。",
    "This collection is missing or was deleted. Refresh and try again.",
  ],
  page_out_of_range: [
    "页码超出这本书的范围。",
    "That page is outside this book.",
  ],
  manifest_missing: [
    "这本书的页面清单尚未生成，请重新扫描书库后再试。",
    "This book's page list is not ready. Scan the library again, then retry.",
  ],
  dynamic_collection: [
    "动态合集的书籍由规则决定；请先转换成静态合集再手动增删。",
    "A dynamic collection follows its rule; convert it to a static collection before adding or removing books.",
  ],
  review_blocked: [
    "请先选择或跳过待定的候选，并解决随书信息冲突，再完成核对。",
    "Choose or skip the pending candidates and resolve the sidecar conflicts before completing the review.",
  ],
  acknowledgement_required: [
    "请逐项确认列出的情况后再完成核对。",
    "Acknowledge each listed item before completing the review.",
  ],
  translation_unsupported: [
    "无法识别该译名数据库，请检查文件或链接。",
    "The translation database was not recognized. Check the file or link.",
  ],
  // Downloads.
  action_unavailable: [
    "此下载当前的状态不支持这个操作，请刷新列表。",
    "This download cannot do that in its current state. Refresh the list.",
  ],
  invalid_batch: [
    "一次提交的链接数量超出范围。",
    "Too many or too few links in one submission.",
  ],
  invalid_mode: [
    "归档类型无效，请重新选择。",
    "The archive type is invalid. Choose again.",
  ],
  price_unavailable: [
    "暂时无法获取归档价格，请稍后重试。",
    "The archive price is unavailable right now. Try again later.",
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
    if (error.code === "source_rate_limited" && error.retryAt)
      return haltMessage(error.retryAt, t);
    if (
      (error.code === "login_rate_limited" || error.code === "login_busy") &&
      error.retryAt
    ) {
      const seconds = Math.max(1, Math.ceil(error.retryAt - Date.now() / 1000));
      return `${codeMessage(error.code, t)} ${t(`请在 ${seconds} 秒后重试。`, `Try again in ${seconds} seconds.`)}`;
    }
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
  // fetch() rejects with a TypeError when no response arrived at all.
  if ((error as { name?: unknown } | null)?.name === "TypeError")
    return t(
      "连接失败，修改可能尚未保存，请重试。",
      "Connection failed. Changes may not be saved. Please retry.",
    );
  return t("操作失败，请重试。", "Something went wrong. Please retry.");
}
