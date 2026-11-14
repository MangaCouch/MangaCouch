import { RouteLink } from "./navigation";
import { restoreScroll } from "./viewState";
import type { GuardRef } from "./navigation";
import { useEffect, useLayoutEffect, useRef, useState } from "react";
import { BookOpen, CloudDownload, Layers, Upload, X } from "lucide-react";
import { Pagination } from "./Controls";
import { aborted, ApiError, errorMessage, request } from "./api";
import { prepareUpload } from "./upload";
import { savedNotice } from "./ReviewDetails";
import { toast } from "./feedback";
import {
  hasFilters,
  LibraryFilters,
  PAGE_SIZE,
  searchParameters,
} from "./LibraryFilters";
import type { Browse } from "./LibraryFilters";
import { Cover } from "./Cover";
import { SearchBox } from "./SearchBox";
import { BookShelf, noSelection } from "./BookShelf";
import { useBookActions } from "./bookActions";
import { useBookTitle } from "./PreferenceContext";
import { preference, remember } from "./device";
import type { Book, SidecarSync, Translate } from "./types";

/** How far down a page of results the reader had scrolled (`query` includes the page). */
export type LibraryScroll = { query: string; y: number };
const lastPage = (total: number) => Math.max(1, Math.ceil(total / PAGE_SIZE));
/** Whether this device last saw books to continue: their row's space is kept while loading. */
const CONTINUE_SHOWN = "continue-shown";
const CONTINUE_PLACEHOLDERS = 4;

/** The library shelf: search, filters, upload, continue reading and paging. */
export function Library({
  t,
  guard,
  navigate,
  browse,
  onBrowse,
  page,
  onPage: setPage,
  scroll,
}: {
  t: Translate;
  guard: GuardRef;
  navigate: (route: string) => void;
  browse: Browse;
  onBrowse: (browse: Browse) => void;
  /** The page of results, kept by the parent so it survives opening a book. */
  page: number;
  onPage: (page: number) => void;
  /** Where the shelf was scrolled, kept by the parent; restored when the same page returns. */
  scroll: { current: LibraryScroll | null };
}) {
  const query = browse.query;
  const title = useBookTitle();
  const [books, setBooks] = useState<Book[]>([]);
  /** The result count and the search (without the page) it counts. */
  const [counted, setCounted] = useState<{
    params: string;
    total: number;
  } | null>(null);
  const countedKey = useRef<{ key: string; total: number } | null>(null);
  const [refresh, setRefresh] = useState(0);
  // null until the first answer; the row then keeps its books while it refreshes.
  const [continuing, setContinuing] = useState<Book[] | null>(null);
  const [continueError, setContinueError] = useState(false);
  const [expectContinue] = useState(
    () => preference(CONTINUE_SHOWN, "true") === "true",
  );
  useEffect(() => {
    const controller = new AbortController();
    void request<{ items: Book[] }>(
      "/api/books?reading=in_progress&order=recent&limit=12&include_total=false",
      { signal: controller.signal },
    )
      .then((value) => {
        if (controller.signal.aborted) return;
        setContinuing(value.items);
        setContinueError(false);
        remember(CONTINUE_SHOWN, String(value.items.length > 0));
      })
      .catch((error) => {
        if (!aborted(error)) setContinueError(true);
      });
    return () => controller.abort();
  }, [refresh]);
  const [busy, setBusy] = useState(true);
  // The query whose books are on screen; a new search or page shows placeholders, a refresh keeps them.
  const [shownQuery, setShownQuery] = useState<string | null>(null);
  const pageQuery = `${searchParameters(browse)}&page=${page}`;
  /** An upload's failure. */
  const [error, setError] = useState<unknown>(null);
  /** Loading the shelf failed. */
  const [listError, setListError] = useState<unknown>(null);
  const [uploading, setUploading] = useState(false);
  const [uploadNote, setUploadNote] = useState("");
  const [existingUpload, setExistingUpload] = useState<string | null>(null);
  const [limit, setLimit] = useState<number | null>(null);
  const upload = useRef<AbortController | null>(null);
  const mounted = useRef(true);
  const file = useRef<HTMLInputElement>(null);
  useEffect(() => {
    mounted.current = true;
    const controller = new AbortController();
    void request<{ max_upload_bytes: number }>("/api/settings", {
      signal: controller.signal,
    })
      .then((value) => {
        if (!controller.signal.aborted) setLimit(value.max_upload_bytes);
      })
      // Without the limit the server still enforces it; the upload just fails later.
      .catch(() => {});
    guard.current = async () => upload.current === null;
    const warn = (event: BeforeUnloadEvent) => {
      if (upload.current) {
        event.preventDefault();
        event.returnValue = "";
      }
    };
    window.addEventListener("beforeunload", warn);
    return () => {
      mounted.current = false;
      controller.abort();
      upload.current?.abort();
      guard.current = null;
      window.removeEventListener("beforeunload", warn);
    };
  }, [guard]);
  useEffect(() => {
    const controller = new AbortController();
    setBusy(true);
    setError(null);
    setListError(null);
    const params = searchParameters(browse);
    const key = `${params}|${refresh}`;
    const known =
      countedKey.current?.key === key ? countedKey.current.total : null;
    void request<{
      items: Book[];
      total: number | null;
    }>(`/api/books?${params}&page=${page}&include_total=${known === null}`, {
      signal: controller.signal,
    })
      .then((value) => {
        if (controller.signal.aborted) return;
        setBooks(value.items);
        setShownQuery(`${params}&page=${page}`);
        const count = value.total ?? known;
        if (count !== null) {
          countedKey.current = { key, total: count };
          setCounted({ params, total: count });
          if (page > lastPage(count)) setPage(lastPage(count));
        }
      })
      .catch((error) => {
        if (!aborted(error)) setListError(error);
      })
      .finally(() => {
        if (!controller.signal.aborted) setBusy(false);
      });
    return () => controller.abort();
  }, [browse, page, refresh, setPage]);
  useLayoutEffect(() => {
    if (busy || shownQuery !== pageQuery) return;
    const saved = scroll.current;
    let restored = false;
    const record = () => {
      if (!restored || location.hash) return;
      scroll.current = { query: pageQuery, y: window.scrollY };
      history.replaceState(
        { ...history.state, mcScroll: scroll.current },
        "",
        location.href,
      );
    };
    const stop = restoreScroll(saved?.query === pageQuery ? saved.y : 0, () => {
      restored = true;
      record();
    });
    window.addEventListener("scroll", record, { passive: true });
    return () => {
      stop();
      window.removeEventListener("scroll", record);
    };
  }, [scroll, pageQuery, busy, shownQuery]);
  // What is on screen belongs to the query it was loaded for: a different search, filter or
  // page that failed shows no books or count of another one; a failed refresh of the same
  // page keeps its books, marked as earlier results.
  const params = searchParameters(browse);
  const current = shownQuery === pageQuery;
  const shownBooks = current ? books : [];
  const total = counted?.params === params ? counted.total : null;
  const stale = !!listError && current;
  // The selection carries across pages; a new search or filter starts over.
  const [selected, setSelected] = useState(() => ({
    key: params,
    value: noSelection(),
  }));
  const selection = selected.key === params ? selected.value : noSelection();
  // Reset during render as well as hiding stale selections: even A -> B -> A must
  // not revive a selection, and no action may observe it under a different query.
  if (selected.key !== params)
    setSelected({ key: params, value: noSelection() });
  const setSelection = (
    next:
      | ReturnType<typeof noSelection>
      | ((
          old: ReturnType<typeof noSelection>,
        ) => ReturnType<typeof noSelection>),
  ) =>
    setSelected((old) => ({
      key: params,
      value:
        typeof next === "function"
          ? next(old.key === params ? old.value : noSelection())
          : next,
    }));
  function changeBrowse(next: Browse) {
    setPage(1);
    scroll.current = null;
    setSelection(noSelection());
    onBrowse(next);
  }
  const actions = useBookActions({
    t,
    navigate,
    changed: () => setRefresh((value) => value + 1),
    // Removed books leave the selection; removing every result ends selecting.
    removed: (ids) =>
      setSelection((current) => {
        if (current.all) return noSelection();
        const picked = new Set(current.picked);
        for (const id of ids) picked.delete(id);
        return picked.size ? { ...current, picked } : noSelection();
      }),
  });
  useEffect(() => {
    const key = (event: KeyboardEvent) => {
      if (
        busy ||
        event.defaultPrevented ||
        event.ctrlKey ||
        event.metaKey ||
        event.shiftKey ||
        event.altKey ||
        document.querySelector(
          "dialog[open],[role=dialog],[role=menu],[role=listbox]",
        )
      )
        return;
      if (
        (event.target as HTMLElement).closest?.(
          "input,textarea,select,button,a,[contenteditable=true]",
        )
      )
        return;
      if (event.key === "ArrowLeft" && page > 1) {
        event.preventDefault();
        setPage(page - 1);
      }
      if (event.key === "ArrowRight" && page < lastPage(total ?? 0)) {
        event.preventDefault();
        setPage(page + 1);
      }
    };
    document.addEventListener("keydown", key);
    return () => document.removeEventListener("keydown", key);
  }, [page, total, busy, setPage]);
  async function importFiles(values: File[]) {
    if (upload.current) return;
    setError(null);
    setUploadNote("");
    setExistingUpload(null);
    let selection;
    try {
      selection = prepareUpload(values, limit, t);
    } catch (error) {
      setUploadNote((error as Error).message);
      if (file.current) file.current.value = "";
      return;
    }
    const controller = new AbortController();
    upload.current = controller;
    setUploading(true);
    try {
      const result = await request<{
        book: Book;
        created: boolean;
        sidecar_sync?: SidecarSync;
      }>(`/api/import?filename=${encodeURIComponent(selection.archive.name)}`, {
        method: "POST",
        body: selection.body,
        signal: controller.signal,
      });
      if (controller.signal.aborted)
        throw new DOMException("Upload stopped", "AbortError");
      if (!mounted.current) return;
      upload.current = null;
      const sync = result.sidecar_sync;
      const syncNotice =
        sync?.state === "pending" || sync?.state === "conflict"
          ? savedNotice(sync, t)
          : "";
      if (!result.created && selection.paired) {
        setExistingUpload(result.book.id);
        setUploadNote(
          t(
            "此书已在书库中，已保留原有信息和阅读状态；本次 JSON 未覆盖它们。",
            "This book already exists. Its saved information and reading state were kept; these JSON files did not overwrite them.",
          ) + (syncNotice ? ` ${syncNotice}` : ""),
        );
      } else {
        if (syncNotice) toast(syncNotice, { duration: 8000 });
        navigate(`book/${result.book.id}`);
      }
    } catch (error) {
      if (!mounted.current) return;
      if (aborted(error)) {
        setUploadNote(
          t(
            "上传已停止；已到达服务器的文件可能仍在入库，可刷新查看。",
            "Upload stopped. A received file may still finish importing; refresh to check.",
          ),
        );
      } else {
        setError(error);
        if (!(error instanceof ApiError) || error.status >= 500)
          setUploadNote(
            t(
              "上传结果未确认，书籍可能已入库。请刷新书架核对；重传同一文件会复用已有记录。",
              "Upload is unconfirmed; the book may already be imported. Refresh to check. Uploading the same file again reuses its record.",
            ),
          );
      }
    } finally {
      if (upload.current === controller) upload.current = null;
      if (mounted.current) {
        setUploading(false);
        if (file.current) file.current.value = "";
      }
    }
  }
  return (
    <main className="library content">
      <header className="library-heading">
        <div>
          <h1>{t("书库", "Library")}</h1>
        </div>
        <div className="actions library-import-actions">
          <RouteLink
            className="icon-button"
            aria-label={t("合集", "Collections")}
            route={"collections"}
            navigate={navigate}
          >
            <Layers size={20} />
          </RouteLink>
          <RouteLink
            className="icon-button"
            aria-label={t("下载", "Downloads")}
            route={"downloads"}
            navigate={navigate}
          >
            <CloudDownload size={20} />
          </RouteLink>
          <button
            type="button"
            className="primary"
            aria-label={t("上传书籍", "Upload books")}
            disabled={uploading}
            onClick={() => file.current?.click()}
          >
            <Upload size={18} />
            {t("上传书籍", "Upload books")}
          </button>
        </div>
        <input
          ref={file}
          hidden
          type="file"
          multiple
          disabled={uploading}
          accept=".zip,.cbz,.pdf,.json"
          onChange={(event) => {
            const values = Array.from(event.target.files ?? []);
            if (values.length) void importFiles(values);
          }}
        />
      </header>
      {uploading && (
        <div className="notice" role="status">
          {t("正在上传并检查文件…", "Uploading and checking the file…")}
          <button type="button" onClick={() => upload.current?.abort()}>
            <X size={15} />
            {t("取消", "Cancel")}
          </button>
        </div>
      )}
      {uploadNote && (
        <p className="notice" role="status">
          {uploadNote}
          {existingUpload && (
            <RouteLink route={`book/${existingUpload}`} navigate={navigate}>
              {t("查看已有书籍", "View existing book")}
            </RouteLink>
          )}
        </p>
      )}
      <div className="library-tools">
        <SearchBox
          value={query}
          search={(value) => changeBrowse({ ...browse, query: value })}
          t={t}
        />
        <LibraryFilters value={browse} change={changeBrowse} t={t} />
      </div>
      {!query &&
        !hasFilters(browse) &&
        continuing === null &&
        expectContinue &&
        !continueError && (
          <section className="continue-section" aria-hidden="true">
            <h2>{t("继续阅读", "Continue reading")}</h2>
            <div className="continue-list">
              {Array.from({ length: CONTINUE_PLACEHOLDERS }, (_, index) => (
                <div className="continue-card skeleton" key={index}>
                  <span className="skeleton-block" />
                  <span>
                    <span className="skeleton-line" />
                    <span className="skeleton-line short" />
                  </span>
                </div>
              ))}
            </div>
          </section>
        )}
      {!query && !hasFilters(browse) && !!continuing?.length && (
        <section
          className="continue-section"
          aria-label={t("继续阅读", "Continue reading")}
        >
          <h2>{t("继续阅读", "Continue reading")}</h2>
          <div className="continue-list">
            {continuing.map((book) => (
              <RouteLink
                className="continue-card"
                key={book.id}
                route={`read/${book.id}`}
                navigate={navigate}
              >
                <Cover
                  id={book.id}
                  sha256={book.content.sha256}
                  revision={book.thumbnail_revision}
                />
                <span>
                  <strong>{title(book)}</strong>
                  <small>
                    {(book.personal.resume_page ?? 0) + 1} /{" "}
                    {book.content.page_count}
                  </small>
                  <progress
                    value={(book.personal.resume_page ?? 0) + 1}
                    max={book.content.page_count}
                  />
                </span>
              </RouteLink>
            ))}
          </div>
        </section>
      )}
      {continueError && (
        <p className="error-text small" role="status">
          {t("继续阅读暂时无法载入。", "Continue reading could not be loaded.")}{" "}
          <button
            type="button"
            className="text-button"
            onClick={() => setRefresh((value) => value + 1)}
          >
            {t("重试", "Retry")}
          </button>
        </p>
      )}
      {!!error && (
        <div role="alert" className="notice error">
          {errorMessage(error, t)}
          <button
            type="button"
            onClick={() => {
              setPage(1);
              setRefresh((value) => value + 1);
            }}
          >
            {t("刷新书架", "Refresh library")}
          </button>
        </div>
      )}
      {!!listError && (
        <div role="alert" className="notice error">
          {stale
            ? t(
                `刷新失败，下面是之前载入的结果：${errorMessage(listError, t)}`,
                `Refreshing failed; the books below are from earlier: ${errorMessage(listError, t)}`,
              )
            : errorMessage(listError, t)}
          <button
            type="button"
            disabled={busy}
            onClick={() => setRefresh((value) => value + 1)}
          >
            {t("重试", "Retry")}
          </button>
        </div>
      )}
      <BookShelf
        key={`${params}:${page}`}
        books={shownBooks}
        total={total}
        browse={browse}
        busy={busy}
        loading={busy && shownQuery !== pageQuery}
        t={t}
        open={(id) => navigate(`book/${id}`)}
        selection={selection}
        select={setSelection}
        menu={actions.menu}
      />
      {actions.dialogs}
      {!busy && !error && !listError && shownBooks.length === 0 && (
        <div className="empty">
          <BookOpen size={36} />
          <h2>
            {query || hasFilters(browse)
              ? t("没有找到匹配的书", "No matching books")
              : t("从一本喜欢的书开始", "Start with a favorite")}
          </h2>
          <p className="muted">
            {query || hasFilters(browse)
              ? t(
                  "试试其他关键词，或清除部分筛选。",
                  "Try other search terms or clear a filter.",
                )
              : t(
                  "添加 ZIP、CBZ 或 PDF，开始阅读。",
                  "Add a ZIP, CBZ or PDF and start reading.",
                )}
          </p>
        </div>
      )}
      <Pagination
        page={page}
        total={total ?? 0}
        size={PAGE_SIZE}
        change={setPage}
        t={t}
        busy={busy}
      />
    </main>
  );
}
