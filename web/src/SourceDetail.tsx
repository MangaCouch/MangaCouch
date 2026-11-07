import { useEffect, useState } from "react";
import { RefreshCw } from "lucide-react";
import { Pagination } from "./Controls";
import { Pill } from "./Pills";
import { toast } from "./feedback";
import { aborted, errorMessage, post, request } from "./api";
import { sourceURL } from "./identifiers";
import { sourceMessage } from "./sourceErrors";
import type {
  Book,
  Source,
  SourceComment,
  SourceNotes,
  Translate,
} from "./types";

/** Scraped source notes and comments; stored per book, outside the curated metadata. */
export interface SourceDiscussion {
  notes: SourceNotes[];
  comments: SourceComment[];
}

/** Loads `GET /api/books/{id}/source-comments`, again whenever the book's metadata revision changes. */
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

const ehLink = (sources: (Source | null | undefined)[]) =>
  sourceURL(
    sources.find(
      (source) => source?.site === "ehentai" || source?.site === "exhentai",
    ),
  ).replace("exhentai.org", "e-hentai.org");
const nhLink = (sources: (Source | null | undefined)[]) =>
  sourceURL(sources.find((source) => source?.site === "nhentai"));

export function SourceRefresh({
  book,
  update,
  t,
}: {
  book: Book;
  update: (book: Book) => void;
  t: Translate;
}) {
  const { data } = useSourceDiscussion(book);
  const sources = [
    book.metadata.source,
    ...(data?.notes ?? []).map((note) => note.source),
  ];
  // Typed links win; untouched fields follow the book's known sources.
  const [typed, setTyped] = useState<{ eh?: string; nh?: string }>({});
  const links = {
    eh: typed.eh ?? ehLink(sources),
    nh: typed.nh ?? nhLink(sources),
  };
  const [busy, setBusy] = useState(false),
    [error, setError] = useState<unknown>(null),
    [notice, setNotice] = useState("");
  return (
    <section className="source-refresh">
      <form
        onSubmit={async (event) => {
          event.preventDefault();
          setBusy(true);
          setError(null);
          setNotice("");
          let fresh = book;
          const warnings: string[] = [];
          try {
            for (const url of [links.eh, links.nh].filter(Boolean)) {
              const result = await post<{
                book: Book;
                comments_error: string | null;
              }>(`/api/books/${encodeURIComponent(book.id)}/source`, {
                url,
                expected_revision: fresh.metadata_revision,
              });
              fresh = result.book;
              update(fresh);
              if (result.comments_error)
                warnings.push(sourceMessage(result.comments_error, t));
            }
            setNotice(warnings.join("; ") || t("已更新", "Updated"));
          } catch (error) {
            setError(error);
          } finally {
            setBusy(false);
          }
        }}
      >
        <label>
          E(x)Hentai
          <input
            type="url"
            value={links.eh}
            disabled={busy}
            placeholder="https://e-hentai.org/g/…"
            onChange={(event) =>
              setTyped((old) => ({
                ...old,
                eh: event.target.value.replace("exhentai.org", "e-hentai.org"),
              }))
            }
          />
        </label>
        <label>
          nHentai
          <input
            type="url"
            value={links.nh}
            disabled={busy}
            placeholder="https://nhentai.net/g/…"
            onChange={(event) =>
              setTyped((old) => ({ ...old, nh: event.target.value }))
            }
          />
        </label>
        {!!error && <p className="notice error">{errorMessage(error, t)}</p>}
        {notice && <p role="status">{notice}</p>}
        <button disabled={busy || !(links.eh || links.nh)}>
          <RefreshCw size={16} className={busy ? "spinner" : ""} />
          {t("从来源更新", "Update from source")}
        </button>
      </form>
    </section>
  );
}

export function SourceComments({ book, t }: { book: Book; t: Translate }) {
  const [page, setPage] = useState(1);
  const [busy, setBusy] = useState(false);
  const { data, error, replace } = useSourceDiscussion(book);
  const notes = data?.notes ?? [],
    comments = data?.comments ?? [];
  const groups = [
    comments.filter((comment) => comment.source.site !== "nhentai"),
    comments.filter((comment) => comment.source.site === "nhentai"),
  ];
  const mixed: SourceComment[] = [];
  for (let i = 0; i < Math.max(...groups.map((group) => group.length)); i++)
    for (const group of groups) {
      const comment = group[i];
      if (comment) mixed.push(comment);
    }
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
              for (const note of notes)
                replace(
                  await post<SourceDiscussion>(
                    `/api/books/${encodeURIComponent(book.id)}/source-comments`,
                    { url: sourceURL(note.source) },
                  ),
                );
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
      {!!error && <p className="notice error">{errorMessage(error, t)}</p>}
      <div className="tags">
        {notes.map((note) => (
          <span className="tags" key={sourceURL(note.source)}>
            <Pill kind="source">
              {note.source.site === "nhentai" ? "nHentai" : "E(x)Hentai"}
            </Pill>
            <Pill>{new Date(note.fetched_at * 1000).toLocaleString()}</Pill>
          </span>
        ))}
      </div>
      {mixed.slice((page - 1) * 20, page * 20).map((comment) => (
        <article key={`${sourceURL(comment.source)}:${comment.comment_id}`}>
          <header>
            <strong className="comment-author">{comment.author}</strong>
            <Pill kind="source">
              {comment.source.site === "nhentai" ? "nHentai" : "E(x)Hentai"}
            </Pill>
            {comment.posted_at && (
              <Pill>
                {new Date(comment.posted_at * 1000).toLocaleDateString()}
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
        page={page}
        total={mixed.length}
        size={20}
        change={setPage}
        t={t}
      />
    </section>
  );
}
