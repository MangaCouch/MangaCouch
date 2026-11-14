import { useState } from "react";
import { RotateCw } from "lucide-react";
import { ApiError, codeMessage, errorMessage, post, request } from "./api";
import { haltEnd, sourceMessage } from "./sourceErrors";
import type { Book, SourceCheck, Translate } from "./types";

/** Check outcomes that are not request errors. */
const checkReasons: Record<string, [string, string]> = {
  source_absent: ["没有来源链接", "No source link is set"],
  metadata_changed: [
    "检查期间元数据被修改",
    "The metadata changed during the check",
  ],
  restart_required: ["服务重启打断了检查", "A restart interrupted the check"],
  interrupted: ["服务重启打断了检查", "A restart interrupted the check"],
};
export const checkReason = (code: string, t: Translate) =>
  checkReasons[code]
    ? t(...checkReasons[code])
    : (codeMessage(code, t) ?? sourceMessage(code, t));

/**
 * What a book's source check tells its owner: a failure (with a retry), or the passing
 * failure it retries by itself. Null when it needs nothing; a check that only needs
 * review shows with the review instead, unless `review` asks for it too.
 */
function checkStatus(
  check: SourceCheck | null | undefined,
  t: Translate,
  review = false,
): { text: string; retry: boolean } | null {
  if (!check) return null;
  const reason = check.error_code ? checkReason(check.error_code, t) : "";
  if (check.state === "needs_review" && (reason || review))
    return {
      text: reason
        ? t(`来源检查失败：${reason}`, `Source check failed: ${reason}`)
        : t(
            "来源检查完成，需核对差异",
            "Source check done; its differences need review",
          ),
      retry: true,
    };
  if (check.state === "queued" && reason && check.retry_at) {
    const at = haltEnd(check.retry_at, t);
    return {
      text: t(
        `来源检查将在 ${at} 左右自动重试（${reason}）`,
        `The source check retries by itself around ${at} (${reason})`,
      ),
      retry: false,
    };
  }
  return null;
}

/**
 * A book's source check status with a retry; `update` receives the book with the new check.
 * A check changed elsewhere meanwhile reloads the book.
 */
export function SourceCheckStatus({
  book,
  update,
  t,
  review = false,
}: {
  book: Book;
  update: (book: Book) => void;
  t: Translate;
  review?: boolean;
}) {
  const [busy, setBusy] = useState(false),
    [error, setError] = useState<unknown>(null),
    // Generation of the check this view queued again: it shows until that run settles.
    [retried, setRetried] = useState<number | null>(null);
  const check = book.source_check;
  const status = checkStatus(check, t, review);
  const waiting =
    !!check &&
    check.generation === retried &&
    (check.state === "queued" || check.state === "running");
  if (!check || (!status && !error && !waiting)) return null;
  async function retry() {
    if (!check) return;
    setBusy(true);
    setError(null);
    const path = `/api/books/${encodeURIComponent(book.id)}`;
    try {
      const fresh = await post<SourceCheck>(
        `${path}/source-check?expected_generation=${check.generation}`,
        { action: "retry" },
      );
      setRetried(fresh.generation);
      update({ ...book, source_check: fresh });
    } catch (failure) {
      setError(failure);
      if (failure instanceof ApiError && failure.status === 409)
        await request<Book>(path).then(update, () => {});
    } finally {
      setBusy(false);
    }
  }
  return (
    <div
      className={`notice source-check${status?.retry ? " error" : ""}`}
      role="status"
    >
      {status && <span>{status.text}</span>}
      {!status && waiting && (
        <span>
          {t(
            "已重新排队，稍后在后台检查。",
            "Queued again; the check runs in the background.",
          )}
        </span>
      )}
      {!!error && <span role="alert">{errorMessage(error, t)}</span>}
      {status?.retry && (
        <button type="button" disabled={busy} onClick={() => void retry()}>
          <RotateCw size={15} className={busy ? "spinner" : ""} />
          {t("重试", "Retry")}
        </button>
      )}
    </div>
  );
}
