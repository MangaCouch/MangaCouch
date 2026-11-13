import type { Translate } from "./types";

/** Stable error codes from the online-source and download endpoints. */
export const sourceErrors: Record<string, [string, string]> = {
  source_restore_paused: [
    "在线请求已暂停，请先核对来源设置再恢复。",
    "Online requests are paused. Check the source settings before resuming.",
  ],
  gallery_unavailable: ["画廊已被下架", "The gallery has been removed."],
  verification_required: [
    "来源已暂停：请在设置中更新 Cookie 和 User-Agent。",
    "Source paused: update Cookie and User-Agent in settings.",
  ],
  source_login_required: [
    "来源登录已失效，请更新 Cookie。",
    "Update the source login cookies.",
  ],
  source_timeout: [
    "来源请求超时，请重试。",
    "The source request timed out. Try again.",
  ],
  source_network: [
    "来源连接失败，可以稍后继续。",
    "Source connection failed. Resume later.",
  ],
  source_rate_limited: [
    "来源限流，请求已暂停，到期后自动继续。",
    "The source is rate limiting; requests are halted and resume by themselves.",
  ],
  source_unavailable: ["来源暂不可用。", "The source is unavailable."],
  source_protocol: [
    "来源返回的数据无法识别。",
    "The source response was not recognized.",
  ],
  source_identity_mismatch: [
    "来源返回了不同的作品，已停止。",
    "The source returned a different gallery.",
  ],
  source_link_expired: [
    "下载链接已过期，请检查可恢复的来源状态。",
    "The download link expired. Check recoverable source state.",
  ],
  source_price_changed: [
    "来源费用已变化，请重新确认。",
    "The source price changed. Confirm again.",
  ],
  insufficient_gp: [
    "来源账户余额不足。",
    "The source account has insufficient funds.",
  ],
  archive_preparing: [
    "来源正在准备归档，稍后可继续。",
    "The source is preparing the archive. Resume shortly.",
  ],
  download_too_large: [
    "归档超过下载大小上限。",
    "The archive exceeds the download size limit.",
  ],
  archive_size_mismatch: [
    "下载文件的图片总大小与来源不符。",
    "The downloaded image size differs from the source.",
  ],
  source_truncated: [
    "下载中断，可以继续。",
    "The transfer was interrupted. Resume to continue.",
  ],
  source_range_invalid: [
    "来源返回了错误的续传范围。",
    "The source returned an invalid download range.",
  ],
  download_file_missing: [
    "临时文件不可用，请继续重新下载。",
    "The temporary file is unavailable. Resume the transfer.",
  ],
  download_local_error: [
    "下载或入库时发生本地错误，详情见服务器日志；排除问题后可继续。",
    "A local error stopped the download or import. See the server log, then resume.",
  ],
  download_invalid_archive: [
    "下载的归档无法入库：文件损坏或含无法识别的页面。文件已保留在服务器的下载目录；“继续”会通过已有链接重新传输，不会再次扣费。",
    "The downloaded archive cannot be imported: it is damaged or has an unreadable page. The file is kept in the server's download folder; Resume transfers it again through the existing link without paying again.",
  ],
  archive_link_invalid: [
    "归档服务器拒绝了链接或返回了无法识别的页面，链接可能已失效。请核对远端结果以获取可用链接。",
    "The archive server refused the link or returned an unrecognised page; the link may be invalid. Check the remote result to get a usable link.",
  ],
  source_redirect_limit: [
    "来源重定向次数过多。",
    "The source redirected too many times.",
  ],
  source_response_too_large: [
    "来源响应超过大小上限。",
    "The source response exceeded the size limit.",
  ],
  unsafe_source_url: ["来源链接不受支持。", "The source URL is not supported."],
  source_private_address: [
    "来源域名解析到了内网地址，请求已被拒绝。请在来源设置中填写代理地址；如使用 Clash/Surge 等 TUN 的 fake-IP 模式，可开启“允许 fake-IP DNS”。",
    "The source host resolved to a private network address, so the request was refused. Set a proxy URL in source settings, or allow fake-IP DNS answers if you use a TUN proxy such as Clash or Surge.",
  ],
  source_absent: [
    "这本书没有来源链接，无法核对元数据。",
    "This book has no source link, so its metadata cannot be reviewed.",
  ],
  source_check_failed: [
    "来源核对失败，请查看服务器日志后重试。",
    "The source check failed. Check the server log and retry.",
  ],
  // Local failures a download or its import can end with.
  insufficient_storage: [
    "磁盘剩余空间不足，下载队列等待中；每 5 分钟检查并自动恢复。",
    "Downloads are waiting for disk space; checked every 5 minutes and resumed automatically.",
  ],
  root_unavailable: [
    "书库目录暂不可用（可能未挂载或无权限），下载已停止；恢复后可继续。",
    "The library folder is unavailable (perhaps unmounted or not writable), so the download stopped. Resume once it is back.",
  ],
  database_busy: [
    "书库数据库正忙（同时处理的任务较多），下载已排队，稍后自动重试；若已停止，稍后点继续即可。",
    "The library database is busy with other work. The download is queued and retries by itself; if it stopped, resume it later.",
  ],
  invalid_url: ["无法识别的链接。", "Not a recognized gallery link."],
  unsupported_source: [
    "不支持该网站，请粘贴 E-Hentai 或 ExHentai 链接。",
    "Unsupported site. Paste an E-Hentai or ExHentai link.",
  ],
  source_retired: [
    "该来源的在线服务已停用，本地书籍和元数据会保留。",
    "Online services for this source were retired. Local books and metadata are kept.",
  ],
  source_key_lost: [
    "来源凭据的密钥丢失或与数据库不匹配，在线来源已停用。请在“设置 → 来源账户与下载”重置来源凭据。",
    "The source credential key is missing or does not match the database, so online sources are off. Reset the source credentials in Settings → Source accounts & downloads.",
  ],
};

/** When a source halt ends (epoch seconds): the time, with the date unless it is today. */
export function haltEnd(seconds: number, t: Translate, now = Date.now()) {
  const end = new Date(seconds * 1000);
  return end.toLocaleString(t("zh-CN", "en-US"), {
    ...(end.toDateString() === new Date(now).toDateString()
      ? {}
      : { month: "short", day: "numeric" }),
    hour: "2-digit",
    minute: "2-digit",
  });
}

/** Source work a rate limit put off: the server queued it and finishes it by itself. */
export function queuedMessage(retryAt: number, t: Translate) {
  const at = haltEnd(retryAt, t);
  return t(
    `来源限流中，已排队，约 ${at} 在后台自动完成（关闭页面也会继续）。`,
    `The source is rate limiting. Queued: it finishes by itself in the background around ${at}, even if you close this page.`,
  );
}

/** source_rate_limited with the end of the site's halt, when the server sent it. */
export function haltMessage(retryAt: number, t: Translate) {
  const at = haltEnd(retryAt, t);
  return t(
    `来源限流，请求已暂停至 ${at}，到期后可重试。`,
    `The source is rate limiting; requests are halted until ${at}. Retry after that.`,
  );
}

/** Localized text for a source error code stored on a job or returned in a result. */
export function sourceMessage(code: string, t: Translate, automatic = false) {
  if (automatic) {
    const messages: Record<string, [string, string]> = {
      source_network: ["来源连接失败。", "Source connection failed."],
      archive_preparing: [
        "来源正在准备归档。",
        "The source is preparing the archive.",
      ],
      root_unavailable: [
        "书库目录暂不可用。",
        "The library folder is unavailable.",
      ],
      archive_link_invalid: [
        "归档链接不可用，正在恢复。",
        "Archive link unavailable; recovering.",
      ],
      source_link_expired: [
        "归档链接已过期，正在恢复。",
        "Archive link expired; recovering.",
      ],
    };
    if (messages[code]) return t(...messages[code]);
  }
  const text = sourceErrors[code];
  return text
    ? t(...text)
    : t(
        "来源操作失败，请稍后重试。",
        "Source operation failed. Try again later.",
      );
}
