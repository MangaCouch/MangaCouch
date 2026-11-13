import { useEffect, useState } from "react";
import { Check } from "lucide-react";
import { SkeletonRows } from "./Controls";
import { Modal } from "./Modal";
import { Pill } from "./Pills";
import { SourceRefresh } from "./SourceDetail";
import {
  Candidate,
  ImportNotes,
  SidecarConflict,
  reviewIssue,
  savedNotice,
  sizeBasis,
} from "./ReviewDetails";
import { useAction } from "./action";
import { ApiError, aborted, errorMessage, post, request } from "./api";
import { exactBytes } from "./format";
import type {
  Book,
  Conflict,
  ReviewPreview,
  SidecarSync,
  SourceNotes,
  Translate,
} from "./types";

export function ReviewDialog({
  book,
  notes,
  update,
  t,
  close,
}: {
  book: Book;
  /** The book's stored source notes, for the source refresh form. */
  notes: SourceNotes[];
  update: (book: Book) => void;
  t: Translate;
  close: () => void;
}) {
  const action = useAction();
  const { error: reviewError, setError: setReviewError } = action;
  const [refreshing, setRefreshing] = useState(true);
  const busy = action.busy || refreshing;
  const [preview, setPreview] = useState<ReviewPreview | null>(null);
  const [conflict, setConflict] = useState<Conflict | null>(null);
  const [accepted, setAccepted] = useState<string[]>([]);
  const [reviewNotice, setReviewNotice] = useState("");
  const [refresh, setRefresh] = useState(0);
  useEffect(() => {
    const controller = new AbortController();
    setRefreshing(true);
    void Promise.all([
      request<ReviewPreview>(`/api/books/${book.id}/review`, {
        signal: controller.signal,
      }),
      request<{ items: Conflict[] }>(
        `/api/sidecar/conflicts?book_id=${book.id}`,
        { signal: controller.signal },
      ),
    ])
      .then(([value, conflicts]) => {
        if (controller.signal.aborted) return;
        setPreview(value);
        setReviewError(null);
        setRefreshing(false);
        setConflict(conflicts.items[0] ?? null);
        setAccepted([]);
      })
      .catch((error) => {
        if (!aborted(error)) {
          setReviewError(error);
          setRefreshing(false);
        }
      });
    return () => controller.abort();
  }, [book.id, book.metadata_revision, refresh, setReviewError]);
  /** Runs a review-dialog action; its failure is shown with a "Refresh review" retry. */
  async function act(task: () => Promise<void>) {
    if (busy) return;
    setRefreshing(true);
    setReviewNotice("");
    if (await action.run(task)) setRefresh((value) => value + 1);
    else setRefreshing(false);
  }
  /** Applies a saved book; a sidecar file not yet written (or in conflict) is said so. */
  function applied(result: Book & { sidecar_sync?: SidecarSync }) {
    update(result);
    const state = result.sidecar_sync?.state;
    if (state === "pending" || state === "conflict")
      setReviewNotice(savedNotice(result.sidecar_sync, t));
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
            disabled={busy}
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
      <SourceRefresh book={book} notes={notes} update={update} t={t} />
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
                {t("实际比较大小", "Actual compared size")} (
                {sizeBasis(preview, t)})
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
              busy={busy}
              t={t}
              submit={(name, choices) =>
                void act(async () =>
                  applied(
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
                {preview.confirmation.action === "manual_confirm"
                  ? t("已记录你的核对结果", "Your review choices are recorded")
                  : preview.confirmation.rule === "local-confirmation/1"
                    ? t(
                        "已复用匹配的本地确认记录",
                        "A matching local confirmation was reused",
                      )
                    : t(
                        "已按在线来源自动确认",
                        "Confirmed automatically from the online source",
                      )}
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
                  disabled={busy}
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
                className="primary review-complete"
                disabled={
                  busy || blocked || accepted.length !== preview.issues.length
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
            </>
          )}
        </>
      ) : (
        <SkeletonRows label={t("加载核对信息…", "Loading review…")} />
      )}
      {conflict && (
        <SidecarConflict
          key={conflict.id}
          conflict={conflict}
          busy={busy}
          t={t}
          submit={(choices) =>
            void act(async () => {
              try {
                applied(
                  await post<Book>(
                    `/api/sidecar/conflicts/${conflict.id}/resolve`,
                    { fields: choices },
                  ),
                );
              } catch (error) {
                if (error instanceof ApiError && error.body?.conflict)
                  setConflict(error.body.conflict as Conflict);
                throw error;
              }
            })
          }
        />
      )}
    </Modal>
  );
}
