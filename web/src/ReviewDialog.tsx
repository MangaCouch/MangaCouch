import { useEffect, useState } from "react";
import { Check } from "lucide-react";
import { Modal } from "./Modal";
import { Pill } from "./Pills";
import { SourceRefresh } from "./SourceDetail";
import {
  Candidate,
  ImportNotes,
  SidecarConflict,
  reviewIssue,
} from "./ReviewDetails";
import { aborted, errorMessage, post, request } from "./api";
import { exactBytes } from "./format";
import type { Book, Conflict, ReviewPreview, Translate } from "./types";

export function ReviewDialog({
  book,
  update,
  t,
  changed,
  close,
}: {
  book: Book;
  update: (book: Book) => void;
  t: Translate;
  changed: boolean;
  close: () => void;
}) {
  const [busy, setBusy] = useState(false);
  const [preview, setPreview] = useState<ReviewPreview | null>(null);
  const [conflict, setConflict] = useState<Conflict | null>(null);
  const [accepted, setAccepted] = useState<string[]>([]);
  const [reviewNotice, setReviewNotice] = useState("");
  const [reviewError, setReviewError] = useState<unknown>(null);
  const [refresh, setRefresh] = useState(0);
  useEffect(() => {
    const controller = new AbortController();
    void Promise.all([
      request<ReviewPreview>(`/api/books/${book.id}/review`, {
        signal: controller.signal,
      }),
      request<{ conflicts: Conflict[] }>(
        `/api/sidecar/conflicts?book_id=${book.id}`,
        { signal: controller.signal },
      ),
    ])
      .then(([value, conflicts]) => {
        if (controller.signal.aborted) return;
        setPreview(value);
        setConflict(conflicts.conflicts[0] ?? null);
        setAccepted([]);
      })
      .catch((error) => {
        if (!aborted(error)) setReviewError(error);
      });
    return () => controller.abort();
  }, [book.id, book.metadata_revision, refresh]);
  /** Runs a review-dialog action; its failure is shown with a "Refresh review" retry. */
  async function act(action: () => Promise<void>) {
    setBusy(true);
    setReviewNotice("");
    setReviewError(null);
    try {
      await action();
      setRefresh((value) => value + 1);
    } catch (error) {
      setReviewError(error);
    } finally {
      setBusy(false);
    }
  }
  const blocked = preview?.issues.some((issue) =>
    ["candidates_unresolved", "sidecar_conflicts"].includes(issue),
  );
  const reviewFeedback = (
    <>
      {!!reviewError && (
        <div role="alert" className="notice error">
          {errorMessage(reviewError, t)}{" "}
          <button
            type="button"
            disabled={busy || changed}
            onClick={() => {
              setReviewError(null);
              setRefresh((value) => value + 1);
            }}
          >
            {t("刷新核对信息", "Refresh review")}
          </button>
        </div>
      )}
      {reviewNotice && (
        <p role="status" className="notice">
          {reviewNotice}
        </p>
      )}
    </>
  );
  return (
    <Modal
      dismissOutside={false}
      title={t("核对信息", "Metadata review")}
      closeLabel={t("关闭", "Close")}
      busy={busy}
      close={close}
    >
      {!preview && reviewFeedback}
      <SourceRefresh book={book} update={update} t={t} />
      {preview ? (
        <>
          <ImportNotes preview={preview} t={t} />
          {preview.issues.includes("declared_byte_size_mismatch") && (
            <dl className="review-sizes">
              <dt>{t("来源声明大小", "Declared source size")}</dt>
              <dd>
                {typeof preview.metadata.declared_byte_size === "number"
                  ? exactBytes(preview.metadata.declared_byte_size)
                  : t("未提供", "Not provided")}
              </dd>
              <dt>
                {preview.byte_size_basis === "images"
                  ? t(
                      "实际图片总大小（解压后）",
                      "Actual image total (uncompressed)",
                    )
                  : t("实际比较大小", "Actual compared size")}
              </dt>
              <dd>
                {preview.compared_byte_size == null
                  ? t("不可用", "Unavailable")
                  : exactBytes(preview.compared_byte_size)}
              </dd>
              <dt>{t("压缩包 / 文件大小", "Archive / file size")}</dt>
              <dd>
                {preview.actual_byte_size == null
                  ? t("未知", "Unknown")
                  : exactBytes(preview.actual_byte_size)}
              </dd>
            </dl>
          )}
          {Object.entries(preview.candidates).map(([name, candidate]) => (
            <Candidate
              key={`${preview.token}:${name}`}
              name={name}
              data={candidate}
              current={preview.metadata}
              busy={busy || changed}
              t={t}
              submit={(name, choices) =>
                void act(async () =>
                  update(
                    await post<Book>(`/api/books/${book.id}/review/candidate`, {
                      token: preview.token,
                      candidate: name,
                      choices,
                    }),
                  ),
                )
              }
            />
          ))}
          {preview.state === "confirmed" && preview.confirmation ? (
            <div>
              <p className="review-confirmed" role="status">
                <Check size={17} />
                {preview.confirmation.evidence_event_id
                  ? t(
                      "已复用匹配的本地确认记录",
                      "A matching local confirmation was reused",
                    )
                  : t("已记录你的核对结果", "Your review choices are recorded")}
              </p>
              {preview.confirmation.acknowledged_issues.length > 0 && (
                <p className="muted small">
                  {t("保留的人工接受项：", "Acknowledged items retained: ")}
                  {preview.confirmation.acknowledged_issues.map((issue) => (
                    <Pill key={issue}>{reviewIssue(issue, preview, t)}</Pill>
                  ))}
                </p>
              )}
              {reviewFeedback}
            </div>
          ) : (
            <>
              <div className="automatic-review">
                <h3>{t("自动核对信息", "Automatic review")}</h3>
                {preview.automatic.reasons.length > 0 ? (
                  <ul>
                    {preview.automatic.reasons.map((reason) => (
                      <li key={reason}>{reviewIssue(reason, preview, t)}</li>
                    ))}
                  </ul>
                ) : (
                  <p>
                    {t(
                      "找到匹配记录，执行时会再次检查原始文件。",
                      "A matching record exists; the archive will be checked again.",
                    )}
                  </p>
                )}
                <button
                  type="button"
                  className="secondary"
                  disabled={busy || changed}
                  onClick={() =>
                    void act(async () => {
                      const result = await post<{
                        book: Book;
                        outcome: string;
                        reasons: string[];
                      }>(`/api/books/${book.id}/review/automatic`, {
                        token: preview.token,
                      });
                      update(result.book);
                      setReviewNotice(
                        result.outcome === "needs_review"
                          ? t("仍需人工核对：", "Manual review is needed: ") +
                              result.reasons
                                .map((reason) =>
                                  reviewIssue(reason, preview, t),
                                )
                                .join("; ")
                          : t("本地核对完成。", "Local review complete."),
                      );
                    })
                  }
                >
                  {t("运行自动核对", "Run automatic review")}
                </button>
                {reviewFeedback}
              </div>
              <h3>{t("确认以下情况", "Acknowledge these items")}</h3>
              {preview.issues.map((issue) => (
                <label className="checkbox issue" key={issue}>
                  <input
                    type="checkbox"
                    disabled={[
                      "candidates_unresolved",
                      "sidecar_conflicts",
                    ].includes(issue)}
                    checked={accepted.includes(issue)}
                    onChange={(event) =>
                      setAccepted((old) =>
                        event.target.checked
                          ? [...old, issue]
                          : old.filter((value) => value !== issue),
                      )
                    }
                  />
                  {reviewIssue(issue, preview, t)}
                </label>
              ))}
              <button
                type="button"
                disabled={
                  busy ||
                  changed ||
                  blocked ||
                  accepted.length !== preview.issues.length
                }
                onClick={() =>
                  void act(async () =>
                    update(
                      await post<Book>(
                        `/api/books/${book.id}/review/complete`,
                        {
                          token: preview.token,
                          acknowledged_issues: accepted,
                        },
                      ),
                    ),
                  )
                }
              >
                {t("完成核对", "Complete review")}
              </button>
              {changed && (
                <p className="muted small">
                  {t(
                    "请先保存上方信息。",
                    "Save metadata before completing the review.",
                  )}
                </p>
              )}
            </>
          )}
        </>
      ) : (
        <p role="status">{t("加载核对信息…", "Loading review…")}</p>
      )}
      {conflict && (
        <SidecarConflict
          key={conflict.id}
          conflict={conflict}
          busy={busy || changed}
          t={t}
          submit={(choices) =>
            void act(async () =>
              update(
                await post<Book>(
                  `/api/sidecar/conflicts/${conflict.id}/resolve`,
                  { fields: choices },
                ),
              ),
            )
          }
        />
      )}
    </Modal>
  );
}
