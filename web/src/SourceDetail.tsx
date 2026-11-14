import { useEffect, useState } from "react";
import { RefreshCw } from "lucide-react";
import { Pagination } from "./Controls";
import { Pill } from "./Pills";
import { ErrorNotice, toast } from "./feedback";
import { ApiError, aborted, errorMessage, post, request } from "./api";
import { dateTime } from "./format";
import { SITE_LABEL, sourceURL } from "./identifiers";
import { queuedMessage, sourceMessage } from "./sourceErrors";
import { checkReason, SourceCheckStatus } from "./SourceCheckStatus";
import { savedNotice } from "./ReviewDetails";
import type {
  Book,
  Source,
  SourceComment,
  SourceNotes,
  SidecarSync,
  Translate,
} from "./types";

/** Scraped source notes and comments; stored per book, outside the curated metadata. */
interface SourceDiscussion {
  notes: SourceNotes[];
  comments: SourceComment[];
  /** Set when a rate limit put the fetch off: the server runs it by itself then. */
  retry_at?: number;
}
/** A comment fetch a rate limit stopped is fetched again in the background by itself. */
const commentsWarning = (code: string, t: Translate) =>
  code === "source_rate_limited"
    ? t(
        "评论被限流，稍后会在后台自动补取。",
        "Comments were rate limited; they are fetched in the background later.",
      )
    : sourceMessage(code, t);

/**
 * Loads `GET /api/books/{id}/source-comments`, again whenever the book's metadata revision
 * changes. The detail page loads it once for its comments and the source refresh form.
 */
export function useSourceDiscussion(book: Book) {
  const [state, setState] = useState<{
    data: SourceDiscussion | null;
    error: unknown;
  }>({ data: null, error: null });
  useEffect(() => {
    const controller = new AbortController();
    request<SourceDiscussion>(
      `/api/books/${encodeURIComponent(book.id)}/source-comments`,
      { signal: controller.signal },
    ).then(
      (data) => setState({ data, error: null }),
      (error) => {
        if (!aborted(error) && !controller.signal.aborted)
          setState((old) => ({ data: old.data, error }));
      },
    );
    return () => controller.abort();
  }, [book.id, book.metadata_revision]);
  return {
    ...state,
    replace: (data: SourceDiscussion) => setState({ data, error: null }),
  };
}
type Discussion = ReturnType<typeof useSourceDiscussion>;

const ehLink = (sources: (Source | null | undefined)[]) =>
  sourceURL(
    sources.find(
      (source) => source?.site === "ehentai" || source?.site === "exhentai",
    ),
  ).replace("exhentai.org", "e-hentai.org");
export function SourceRefresh({
  book,
  notes,
  update,
  t,
}: {
  book: Book;
  /** Stored source notes provide the gallery link when metadata has none. */
  notes: SourceNotes[];
  update: (book: Book) => void;
  t: Translate;
}) {
  const sources = [book.metadata.source, ...notes.map((note) => note.source)];
  const [typed, setTyped] = useState<string>();
  const link = typed ?? ehLink(sources);
  const [busy, setBusy] = useState(false),
    [error, setError] = useState<unknown>(null),
    [notice, setNotice] = useState("");
  return (
    <section className="source-refresh">
      <SourceCheckStatus book={book} update={update} t={t} review />
      <form
        onSubmit={async (event) => {
          event.preventDefault();
          setBusy(true);
          setError(null);
          setNotice("");
          const warnings: string[] = [];
          try {
            if (link) {
              const result = await post<{
                book: Book;
                comments_error: string | null;
                retry_at?: number;
                sync?: SidecarSync;
              }>(
                `/api/books/${encodeURIComponent(book.id)}/source?expected_revision=${book.metadata_revision}`,
                { url: link },
              );
              update(result.book);
              if (result.retry_at)
                warnings.push(queuedMessage(result.retry_at, t));
              else if (result.comments_error)
                warnings.push(commentsWarning(result.comments_error, t));
              if (
                result.sync?.state === "pending" ||
                result.sync?.state === "conflict"
              )
                warnings.push(savedNotice(result.sync, t));
              setNotice(
                [...new Set(warnings)].join("; ") || t("已更新", "Updated"),
              );
            }
          } catch (error) {
            setError(error);
            if (error instanceof ApiError && error.status === 409) {
              await request<Book>(
                `/api/books/${encodeURIComponent(book.id)}`,
              ).then(update, () => {});
              setNotice(
                t(
                  "书籍已更新，请重试一次。",
                  "The book was updated; please retry.",
                ),
              );
            }
          } finally {
            setBusy(false);
          }
        }}
      >
        <label>
          E(x)Hentai
          <input
            type="url"
            value={link}
            disabled={busy}
            placeholder="https://e-hentai.org/g/…"
            onChange={(event) => setTyped(event.target.value)}
          />
        </label>
        <ErrorNotice error={error} t={t} />
        {notice && <p role="status">{notice}</p>}
        <button disabled={busy || !link}>
          <RefreshCw size={16} className={busy ? "spinner" : ""} />
          {t("从来源核对", "Review against source")}
        </button>
      </form>
    </section>
  );
}

export function SourceComments({
  book,
  discussion,
  t,
}: {
  book: Book;
  discussion: Discussion;
  t: Translate;
}) {
  const [page, setPage] = useState(1);
  const [busy, setBusy] = useState(false);
  const { data, error, replace } = discussion;
  const locale = t("zh-CN", "en-US");
  const notes = data?.notes ?? [],
    comments = data?.comments ?? [];
  const currentPage = Math.min(
    page,
    Math.max(1, Math.ceil(comments.length / 20)),
  );
  useEffect(() => {
    if (data && page !== currentPage) setPage(currentPage);
  }, [data, page, currentPage]);
  if (!error && !comments.length && !notes.length) return null;
  return (
    <section className="panel source-comments">
      <header className="section-head">
        <h2>{t("来源评论", "Source comments")}</h2>
        <button
          className="icon-button"
          aria-label={t("更新来源评论", "Update source comments")}
          disabled={busy || !notes.length}
          onClick={async () => {
            setBusy(true);
            try {
              // Comments live outside the metadata: the book itself does not change.
              let queued: number | undefined;
              for (const note of notes) {
                const result = await post<SourceDiscussion>(
                  `/api/books/${encodeURIComponent(book.id)}/source-comments`,
                  { url: sourceURL(note.source) },
                );
                replace(result);
                queued = result.retry_at ?? queued;
              }
              if (queued) toast.info(queuedMessage(queued, t));
            } catch (error) {
              toast.error(errorMessage(error, t));
            } finally {
              setBusy(false);
            }
          }}
        >
          <RefreshCw size={17} className={busy ? "spinner" : ""} />
        </button>
      </header>
      <ErrorNotice error={error} t={t} />
      {notes.map(
        ({ source, comments_failure: failure }) =>
          failure && (
            <p className="notice error" key={`failure:${sourceURL(source)}`}>
              <Pill kind="source">{SITE_LABEL}</Pill>
              {t(
                `上次补取失败：${checkReason(failure.code, t)}`,
                `The last background fetch failed: ${checkReason(failure.code, t)}`,
              )}
            </p>
          ),
      )}
      <div className="tags">
        {notes.map((note) => (
          <span className="tags" key={sourceURL(note.source)}>
            <Pill kind="source">{SITE_LABEL}</Pill>
            {note.fetched_at !== null && (
              <Pill>{dateTime(note.fetched_at, locale)}</Pill>
            )}
          </span>
        ))}
      </div>
      {comments
        .slice((currentPage - 1) * 20, currentPage * 20)
        .map((comment) => (
          <article key={`${sourceURL(comment.source)}:${comment.comment_id}`}>
            <header>
              <strong className="comment-author">{comment.author}</strong>
              <Pill kind="source">{SITE_LABEL}</Pill>
              {comment.posted_at && (
                <Pill>
                  {dateTime(comment.posted_at, locale, { dateStyle: "medium" })}
                </Pill>
              )}
              {comment.score != null && <Pill>{comment.score}</Pill>}
            </header>
            <p>{comment.body}</p>
          </article>
        ))}
      {!error && !comments.length && (
        <p className="muted">{t("暂无评论", "No comments")}</p>
      )}
      <Pagination
        page={currentPage}
        total={comments.length}
        size={20}
        change={setPage}
        t={t}
      />
    </section>
  );
}
