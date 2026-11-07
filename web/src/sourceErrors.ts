import type { Translate } from "./types";

/** Stable error codes from the online-source and download endpoints. */
export const sourceErrors: Record<string, [string, string]> = {
  gallery_unavailable: [
    "图库已下架或当前凭据无法访问。",
    "Gallery removed or unavailable to these credentials.",
  ],
  verification_required: [
    "来源已暂停：请在设置中更新 Cookie 和 User-Agent。",
    "Source paused: update Cookie and User-Agent in settings.",
  ],
  source_login_required: [
    "来源登录已失效，请更新 Cookie。",
    "Update the source login cookies.",
  ],
  nh_key_required: [
    "请先在来源设置中填写 nHentai API key。",
    "Set the nHentai API key in source settings.",
  ],
  source_network: [
    "来源连接失败，可以稍后继续。",
    "Source connection failed. Resume later.",
  ],
  source_rate_limited: [
    "来源限流，请稍后继续。",
    "The source is rate limited. Resume later.",
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
  source_search_failed: [
    "E(x)Hentai 搜索失败，请重试或粘贴 E(x)Hentai 链接。",
    "E(x)Hentai search failed. Retry or paste an E(x)Hentai link.",
  ],
  remote_result_unknown: [
    "远端提交结果未确认，请先检查来源下载状态。",
    "The remote submission is unconfirmed. Check its download status first.",
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
    "下载或入库失败，请检查存储后继续。",
    "Download or import failed. Check storage and resume.",
  ],
  source_redirect_limit: [
    "来源重定向次数过多。",
    "The source redirected too many times.",
  ],
  source_response_too_large: [
    "来源响应超过大小上限。",
    "The source response exceeded the size limit.",
  ],
  source_title_missing: [
    "来源未返回标题。",
    "The source did not return a title.",
  ],
  unsafe_source_url: ["来源链接不受支持。", "The source URL is not supported."],
  invalid_url: ["无法识别的链接。", "Not a recognized gallery link."],
  unsupported_source: [
    "不支持该网站，请粘贴 E-Hentai、ExHentai 或 nHentai 链接。",
    "Unsupported site. Paste an E-Hentai, ExHentai or nHentai link.",
  ],
};

/** Localized text for a source error code stored on a job or returned in a result. */
export function sourceMessage(code: string, t: Translate) {
  const text = sourceErrors[code];
  return text
    ? t(...text)
    : t(
        "来源操作失败，请稍后重试。",
        "Source operation failed. Try again later.",
      );
}
