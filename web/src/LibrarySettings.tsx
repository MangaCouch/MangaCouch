import { useViewState } from "./viewState";
import { RouteLink } from "./navigation";
import { useAction } from "./action";
import { useCallback, useEffect, useState } from "react";
import { LoaderCircle, X } from "lucide-react";
import { aborted, post, put, request } from "./api";
import { ErrorNotice } from "./feedback";
import { usePolling } from "./polling";
import { CursorPagination, Pagination, SkeletonRows } from "./Controls";
import { dateTime } from "./format";
import { Cover } from "./Cover";
import { BookPills, Pill } from "./Pills";
import { RemovalDialog } from "./RemovalDialog";
import { useBookTitle } from "./PreferenceContext";
import { SourceCheckStatus } from "./SourceCheckStatus";
import type { Book, Translate } from "./types";

export function SourceReviewList({
  t,
  navigate,
}: {
  t: Translate;
  navigate: (route: string) => void;
}) {
  const title = useBookTitle();
  const [books, setBooks] = useState<Book[]>([]),
    [total, setTotal] = useState(0),
    [page, setPage] = useViewState("source-review:page", 1),
    [shownPage, setShownPage] = useState(0);
  const loaded = shownPage === page;
  const { error } = usePolling(
    async (signal) => {
      const data = await request<{ items: Book[]; total: number }>(
        `/api/books?review=needs_review&limit=20&page=${page}`,
        { signal },
      );
      if (signal.aborted) return;
      const last = Math.max(1, Math.ceil(data.total / 20));
      if (page > last) {
        setPage(last);
        return;
      }
      setBooks(data.items);
      setTotal(data.total);
      setShownPage(page);
    },
    15_000,
    { key: page },
  );
  return (
    <section>
      <ErrorNotice error={error} t={t} />
      {!loaded && !error && (
        <SkeletonRows
          label={t("读取需核对的书…", "Loading books to review…")}
        />
      )}
      {(loaded ? books : []).map((book) => (
        <div className="settings-row" key={book.id}>
          <RouteLink
            className="text-button"
            route={`book/${book.id}`}
            navigate={navigate}
          >
            {title(book)}
          </RouteLink>
        </div>
      ))}
      {loaded && !total && (
        <p className="muted">
          {t("没有需核对的书", "No books awaiting review")}
        </p>
      )}
      {total > 20 && (
        <Pagination
          page={page}
          total={total}
          size={20}
          change={setPage}
          t={t}
        />
      )}
      <SourceCheckList t={t} navigate={navigate} />
    </section>
  );
}

/** Source checks that failed or need review, each with a retry. */
function SourceCheckList({
  t,
  navigate,
}: {
  t: Translate;
  navigate: (route: string) => void;
}) {
  const title = useBookTitle();
  const [books, setBooks] = useState<Book[]>([]),
    [total, setTotal] = useState(0),
    [page, setPage] = useViewState("source-checks:page", 1),
    [shownPage, setShownPage] = useState(0);
  const loaded = shownPage === page;
  const action = useAction();
  const { error, refresh } = usePolling(
    async (signal) => {
      const data = await request<{ items: Book[]; total: number }>(
        `/api/source-checks?limit=20&page=${page}`,
        { signal },
      );
      if (signal.aborted) return;
      const last = Math.max(1, Math.ceil(data.total / 20));
      if (page > last) {
        setPage(last);
        return;
      }
      setBooks(data.items);
      setTotal(data.total);
      setShownPage(page);
    },
    15_000,
    { key: page },
  );
  const replace = (fresh: Book) =>
    setBooks((old) => old.map((book) => (book.id === fresh.id ? fresh : book)));
  return (
    <>
      <h4>{t("未完成的来源检查", "Unfinished source checks")}</h4>
      <ErrorNotice error={error || action.error} t={t} />
      {total > 0 && (
        <button
          disabled={action.busy}
          onClick={() =>
            void action.run(async () => {
              await post("/api/source-checks/retry-failed");
              await refresh();
            })
          }
        >
          {t("重试全部失败的核对", "Retry all failed checks")}
        </button>
      )}
      {!loaded && !error && (
        <SkeletonRows label={t("读取来源检查…", "Loading source checks…")} />
      )}
      {(loaded ? books : []).map((book) => (
        <div className="settings-row source-check-row" key={book.id}>
          <RouteLink
            className="text-button"
            route={`book/${book.id}`}
            navigate={navigate}
          >
            {title(book)}
          </RouteLink>
          <SourceCheckStatus book={book} update={replace} t={t} review />
        </div>
      ))}
      {loaded && !total && (
        <p className="muted">
          {t("没有未完成的来源检查", "No unfinished source checks")}
        </p>
      )}
      {total > 20 && (
        <Pagination
          page={page}
          total={total}
          size={20}
          change={setPage}
          t={t}
        />
      )}
    </>
  );
}

/** Duplicate pairs per page. */
const DUPLICATE_PAGE = 20;
type Group = {
  left_id: string;
  right_id: string;
  books: Book[];
  matched_pages: number;
  coverage: number;
};
export function DuplicateSettings({
  t,
  navigate,
}: {
  t: Translate;
  navigate: (route: string) => void;
}) {
  const title = useBookTitle();
  const [groups, setGroups] = useState<Group[]>([]),
    [busy, setBusy] = useState(false),
    [removing, setRemoving] = useState<Book[] | null>(null),
    [error, setError] = useState<unknown>(null),
    [selected, setSelected] = useState<Record<string, boolean>>({}),
    [indexing, setIndexing] = useState({ indexed: 0, total: 0 }),
    [comparing, setComparing] = useState({ done: 0, total: 0 }),
    [loading, setLoading] = useState(true),
    // Cursors of the pages visited.
    [cursors, setCursors] = useViewState("duplicates:pages", [""]),
    [next, setNext] = useState<string | null>(null);
  const cursor = cursors.at(-1)!;
  const { error: loadError, refresh } = usePolling(
    async (signal) => {
      const data = await request<{
        items: Group[];
        after: string | null;
        comparing: { done: number; total: number };
        indexed_pages: number;
        total_pages: number;
      }>(
        `/api/duplicates?limit=${DUPLICATE_PAGE}&after=${encodeURIComponent(cursor)}`,
        { signal },
      );
      if (signal.aborted) return;
      if (!data.items.length && cursor) {
        setCursors((old) => old.slice(0, -1));
        return;
      }
      setGroups(data.items);
      setNext(data.after);
      setIndexing({ indexed: data.indexed_pages, total: data.total_pages });
      setComparing(data.comparing);
      setLoading(false);
    },
    15_000,
    { key: cursor },
  );
  const turn = (change: (old: string[]) => string[]) => {
    setLoading(true);
    setGroups([]);
    setCursors(change);
  };
  const pairKey = (group: Group) => `${group.left_id}:${group.right_id}`;
  const kept = (group: Group, book: Book) =>
    selected[`${pairKey(group)}:${book.id}`] ?? true;
  /** Hides the pair for good: both copies stay in the library. */
  async function ignore(group: Group) {
    setBusy(true);
    setError(null);
    try {
      await post("/api/duplicates/ignore", {
        left_id: group.left_id,
        right_id: group.right_id,
      });
    } catch (failure) {
      setError(failure);
    } finally {
      setBusy(false);
    }
    await refresh();
  }
  return (
    <section>
      {indexing.total > indexing.indexed && (
        <p className="muted">
          {t("后台检测中", "Checking in the background")}{" "}
          <Pill>
            {indexing.indexed} / {indexing.total} {t("页", "pages")}
          </Pill>
        </p>
      )}
      {indexing.indexed >= indexing.total &&
        comparing.done < comparing.total && (
          <p className="muted">
            {t(
              `比较中 ${comparing.done} / ${comparing.total} 本`,
              `Comparing ${comparing.done} / ${comparing.total} books`,
            )}
          </p>
        )}
      <ErrorNotice error={error ?? loadError} t={t} />
      {!groups.length &&
        (loading ? (
          <SkeletonRows label={t("读取检测结果…", "Loading results…")} />
        ) : comparing.done >= comparing.total &&
          indexing.indexed >= indexing.total ? (
          <p className="muted">
            {t("暂无发现重复本子", "No duplicate books found")}
          </p>
        ) : null)}
      {groups.map((group) => {
        const remove = group.books.filter((book) => !kept(group, book));
        return (
          <article className="duplicate-group" key={pairKey(group)}>
            <Pill>
              {t(
                `${group.matched_pages} 页相似`,
                `${group.matched_pages} similar pages`,
              )}
            </Pill>
            <div className="duplicate-pair">
              {group.books.map((book) => (
                <div key={book.id}>
                  <RouteLink
                    className="duplicate-cover"
                    route={`book/${book.id}`}
                    navigate={navigate}
                  >
                    <Cover
                      id={book.id}
                      sha256={book.content.sha256}
                      revision={book.thumbnail_revision}
                      alt={title(book)}
                    />
                  </RouteLink>
                  <strong>{title(book)}</strong>
                  <BookPills book={book} t={t} />
                  <label className="checkbox">
                    <input
                      type="checkbox"
                      checked={kept(group, book)}
                      onChange={(event) =>
                        setSelected((old) => ({
                          ...old,
                          [`${pairKey(group)}:${book.id}`]:
                            event.target.checked,
                        }))
                      }
                    />
                    {t("保留", "Keep")}
                  </label>
                </div>
              ))}
            </div>
            {remove.length === group.books.length && (
              <p className="notice error" role="alert">
                {t(
                  "所有副本都未勾选保留：应用后这部作品将从书库中全部移除。请至少保留一本。",
                  "No copy is marked Keep: applying would remove this work from the library entirely. Keep at least one copy.",
                )}
              </p>
            )}
            <div className="actions">
              <button
                disabled={busy || remove.length !== 1}
                onClick={() => setRemoving(remove)}
              >
                {t("应用选择", "Apply selection")}
              </button>
              <button disabled={busy} onClick={() => void ignore(group)}>
                {t("忽略", "Ignore")}
              </button>
            </div>
          </article>
        );
      })}
      <CursorPagination
        page={cursors.length}
        more={!!next}
        busy={busy || loading}
        first={() => turn(() => [""])}
        previous={() => turn((old) => old.slice(0, -1))}
        next={() => turn((old) => [...old, next!])}
        t={t}
      />
      {removing && (
        <RemovalDialog
          books={removing}
          t={t}
          close={() => setRemoving(null)}
          removed={() => {
            setRemoving(null);
            void refresh();
          }}
        />
      )}
    </section>
  );
}

type Alias = { namespace: string; value: string; translation: string };
const aliasKey = (item: { namespace: string; value: string }) =>
  `${item.namespace}:${item.value}`;
export function TranslationSettings({ t }: { t: Translate }) {
  const [status, setStatus] = useState<{
      updated_at: number | null;
      count: number;
    } | null>(null),
    [aliases, setAliases] = useState<Alias[]>([]),
    [cursors, setCursors] = useViewState("translations:pages", [""]),
    [next, setNext] = useState<string | null>(null),
    [draft, setDraft] = useState<Record<string, string>>({}),
    [saving, setSaving] = useState<Set<string>>(new Set()),
    [tag, setTag] = useState(""),
    [translation, setTranslation] = useState(""),
    [updating, setUpdating] = useState(false),
    [error, setError] = useState<unknown>(null);
  const cursor = cursors.at(-1)!;
  const load = useCallback(
    async (signal?: AbortSignal) => {
      const [state, page] = await Promise.all([
        request<{ updated_at: number | null; count: number }>(
          "/api/translations",
          { signal },
        ),
        request<{ items: Alias[]; after: string | null }>(
          `/api/tag-aliases?limit=20&after=${encodeURIComponent(cursor)}`,
          { signal },
        ),
      ]);
      if (!signal?.aborted) {
        setStatus(state);
        if (!page.items.length && cursor) {
          setCursors((old) => old.slice(0, -1));
          return;
        }
        setAliases(page.items);
        setNext(page.after);
        setError(null);
      }
    },
    [cursor, setCursors],
  );
  useEffect(() => {
    const c = new AbortController();
    void load(c.signal).catch((error) => {
      if (!aborted(error)) setError(error);
    });
    return () => c.abort();
  }, [load]);
  const mark = (key: string, on: boolean) =>
    setSaving((old) => {
      const next = new Set(old);
      if (on) next.add(key);
      else next.delete(key);
      return next;
    });
  /** Saves one row (empty translation deletes it) and updates only that row afterwards. */
  async function save(item: Alias, value: string, expected: string | null) {
    value = value.trim();
    const key = aliasKey(item);
    if (saving.has(key)) return false;
    mark(key, true);
    setError(null);
    try {
      const saved = await put<Alias | undefined>("/api/tag-aliases", {
        tag: { namespace: item.namespace, value: item.value },
        translation: value,
        expected,
      });
      setDraft(({ [key]: _, ...rest }) => rest);
      if (expected === null) await load();
      else
        setAliases((old) =>
          saved
            ? old.map((row) =>
                aliasKey(row) === key
                  ? { ...row, translation: saved.translation }
                  : row,
              )
            : old.filter((row) => aliasKey(row) !== key),
        );
      if (!saved) await load();
      return true;
    } catch (error) {
      setError(error);
      return false;
    } finally {
      mark(key, false);
    }
  }
  return (
    <section>
      <div className="settings-row">
        <span>
          EhTagTranslation
          <small>
            {status?.updated_at
              ? dateTime(status.updated_at, t("zh-CN", "en-US"))
              : t("尚未更新", "Not downloaded yet")}
          </small>
        </span>
        <button
          disabled={updating}
          onClick={async () => {
            setUpdating(true);
            setError(null);
            try {
              setStatus(await post("/api/translations/update"));
            } catch (error) {
              setError(error);
            } finally {
              setUpdating(false);
            }
          }}
        >
          {updating && <LoaderCircle className="spinner" size={16} />}{" "}
          {t("立即更新", "Update now")}
        </button>
      </div>
      <a
        className="muted small"
        href="https://github.com/EhTagTranslation/Database"
        target="_blank"
        rel="noreferrer"
      >
        EhTagTranslation <Pill>CC BY-NC-SA 3.0</Pill>
      </a>
      <h4>{t("自定义标签译名", "Custom translations")}</h4>
      <ErrorNotice error={error} t={t} />
      {aliases.map((item) => {
        const key = aliasKey(item),
          busy = saving.has(key);
        return (
          <div className="translation-row" key={key}>
            <Pill>{key}</Pill>
            <span>→</span>
            <input
              aria-label={t(`${key} 译名`, `${key} translation`)}
              disabled={busy}
              value={draft[key] ?? item.translation}
              onChange={(event) =>
                setDraft((old) => ({ ...old, [key]: event.target.value }))
              }
              onBlur={() => {
                if (draft[key] !== undefined && draft[key] !== item.translation)
                  void save(item, draft[key]!, item.translation);
              }}
            />
            <button
              className="icon-button close-button danger"
              disabled={busy}
              aria-label={t(`删除 ${key}`, `Delete ${key}`)}
              // Keep focus in the input so its blur-save cannot start and swallow this click.
              onMouseDown={(event) => event.preventDefault()}
              onClick={() => void save(item, "", item.translation)}
            >
              <X size={14} />
            </button>
          </div>
        );
      })}
      <CursorPagination
        page={cursors.length}
        more={!!next}
        first={() => setCursors([""])}
        previous={() => setCursors((old) => old.slice(0, -1))}
        next={() => setCursors((old) => [...old, next!])}
        t={t}
      />
      <form
        className="translation-row"
        onSubmit={(event) => {
          event.preventDefault();
          const colon = tag.indexOf(":");
          const item = {
            namespace: colon < 0 ? "other" : tag.slice(0, colon).trim(),
            value: (colon < 0 ? tag : tag.slice(colon + 1)).trim(),
            translation,
          };
          void save(item, translation, null).then((saved) => {
            if (!saved) return;
            setTag((current) => (current === tag ? "" : current));
            setTranslation((current) =>
              current === translation ? "" : current,
            );
          });
        }}
      >
        <input
          value={tag}
          required
          aria-label={t("新标签", "New tag")}
          placeholder="namespace:value"
          onChange={(event) => setTag(event.target.value)}
        />
        <span>→</span>
        <input
          value={translation}
          required
          aria-label={t("译名", "Translation")}
          placeholder={t("译名", "Translation")}
          onChange={(event) => setTranslation(event.target.value)}
        />
        <button disabled={!tag.trim() || !translation.trim()}>
          {t("添加", "Add")}
        </button>
      </form>
    </section>
  );
}
