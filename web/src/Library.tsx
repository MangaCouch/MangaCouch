import { useEffect, useRef, useState } from "react";
import { BookOpen, CloudDownload, Layers, Upload, X } from "lucide-react";
import { Pagination } from "./Controls";
import { aborted, ApiError, errorMessage, request } from "./api";
import { prepareUpload } from "./upload";
import {
  hasFilters,
  LibraryFilters,
  PAGE_SIZE,
  searchParameters,
} from "./LibraryFilters";
import type { Browse } from "./LibraryFilters";
import { Cover } from "./Cover";
import { SearchBox } from "./SearchBox";
import { BookShelf, type LibrarySelection } from "./BookShelf";
import { useBookTitle } from "./PreferenceContext";
import type { Book, Translate } from "./types";

type Guard = () => Promise<boolean>;
const lastPage = (total: number) => Math.max(1, Math.ceil(total / PAGE_SIZE));

/** The library shelf: search, filters, upload, continue reading and paging. */
export function Library({
  t,
  guard,
  navigate,
  browse,
  onBrowse,
  onOrganize,
  selectRequested,
  onSelectionStarted,
  version,
}: {
  t: Translate;
  guard: { current: Guard | null };
  navigate: (route: string) => void;
  browse: Browse;
  onBrowse: (browse: Browse) => void;
  onOrganize: (selection: LibrarySelection) => void;
  selectRequested: boolean;
  onSelectionStarted: () => void;
  /** Bumped by the parent when books changed elsewhere (e.g. a bulk action). */
  version: number;
}) {
  const query = browse.query;
  const title = useBookTitle();
  const [books, setBooks] = useState<Book[]>([]);
  const [total, setTotal] = useState<number | null>(null);
  const counted = useRef<{ key: string; total: number } | null>(null);
  const [refresh, setRefresh] = useState(0);
  const [continuing, setContinuing] = useState<Book[]>([]);
  const [continueError, setContinueError] = useState(false);
  useEffect(() => {
    const controller = new AbortController();
    void request<{ books: Book[] }>(
      "/api/books?reading=in_progress&order=recent&limit=12",
      { signal: controller.signal },
    )
      .then((value) => {
        if (controller.signal.aborted) return;
        setContinuing(value.books);
        setContinueError(false);
      })
      .catch((error) => {
        if (!aborted(error)) setContinueError(true);
      });
    return () => controller.abort();
  }, [refresh, version]);
  const [page, setPage] = useState(1);
  const [busy, setBusy] = useState(true);
  const [error, setError] = useState<unknown>(null);
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
    const params = searchParameters(browse, "");
    const key = `${params}|${refresh}|${version}`;
    const known = counted.current?.key === key ? counted.current.total : null;
    void request<{
      books: Book[];
      after_id: string | null;
      total: number | null;
    }>(`/api/books?${params}&page=${page}&include_total=${known === null}`, {
      signal: controller.signal,
    })
      .then((value) => {
        if (controller.signal.aborted) return;
        setBooks(value.books);
        const count = value.total ?? known;
        if (count !== null) {
          counted.current = { key, total: count };
          setTotal(count);
          if (page > lastPage(count)) setPage(lastPage(count));
        }
      })
      .catch((error) => {
        if (!aborted(error)) setError(error);
      })
      .finally(() => {
        if (!controller.signal.aborted) setBusy(false);
      });
    return () => controller.abort();
  }, [browse, page, refresh, version]);
  function changeBrowse(next: Browse) {
    setPage(1);
    onBrowse(next);
  }
  useEffect(() => {
    const key = (event: KeyboardEvent) => {
      if (
        busy ||
        event.defaultPrevented ||
        event.ctrlKey ||
        event.metaKey ||
        event.shiftKey ||
        document.querySelector("dialog[open],[role=dialog],[role=menu]")
      )
        return;
      if (
        (event.target as HTMLElement).closest?.(
          "input,textarea,select,button,[contenteditable=true]",
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
  }, [page, total, busy]);
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
      const result = await request<{ book: Book; created: boolean }>(
        `/api/import?filename=${encodeURIComponent(selection.archive.name)}`,
        { method: "POST", body: selection.body, signal: controller.signal },
      );
      if (controller.signal.aborted)
        throw new DOMException("Upload stopped", "AbortError");
      if (!mounted.current) return;
      upload.current = null;
      if (!result.created && selection.paired) {
        setExistingUpload(result.book.id);
        setUploadNote(
          t(
            "此书已在书库中，已保留原有信息和阅读状态；本次 JSON 未覆盖它们。",
            "This book already exists. Its saved information and reading state were kept; these JSON files did not overwrite them.",
          ),
        );
      } else navigate(`book/${result.book.id}`);
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
          <button
            type="button"
            className="icon-button"
            aria-label={t("合集", "Collections")}
            onClick={() => navigate("collections")}
          >
            <Layers size={20} />
          </button>
          <button
            type="button"
            className="icon-button"
            aria-label={t("下载", "Downloads")}
            onClick={() => navigate("downloads")}
          >
            <CloudDownload size={20} />
          </button>
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
            <button
              type="button"
              onClick={() => navigate(`book/${existingUpload}`)}
            >
              {t("查看已有书籍", "View existing book")}
            </button>
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
      {!query && !hasFilters(browse) && continuing.length > 0 && (
        <section
          className="continue-section"
          aria-label={t("继续阅读", "Continue reading")}
        >
          <h2>{t("继续阅读", "Continue reading")}</h2>
          <div className="continue-list">
            {continuing.map((book) => (
              <button
                type="button"
                className="continue-card"
                key={book.id}
                onClick={() => navigate(`read/${book.id}`)}
              >
                <Cover id={book.id} sha256={book.content.sha256} />
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
              </button>
            ))}
          </div>
        </section>
      )}
      {continueError && (
        <p className="error-text small" role="status">
          {t("继续阅读暂时无法载入。", "Continue reading could not be loaded.")}
        </p>
      )}
      {!!error && (
        <div role="alert" className="notice error">
          {errorMessage(error, t)}
          <button
            type="button"
            onClick={() => {
              setBooks([]);
              setPage(1);
              setRefresh((value) => value + 1);
            }}
          >
            {t("刷新书架", "Refresh library")}
          </button>
        </div>
      )}
      <BookShelf
        key={`${searchParameters(browse, "")}:${page}:${refresh}:${version}`}
        books={books}
        total={total}
        browse={browse}
        busy={busy}
        t={t}
        open={(id) => navigate(`book/${id}`)}
        organize={onOrganize}
        selectRequested={selectRequested}
        onSelectionStarted={onSelectionStarted}
      />
      {!busy && !error && books.length === 0 && (
        <div className="empty">
          <BookOpen size={36} />
          <h2>
            {query || hasFilters(browse)
              ? t("没有找到匹配的本子", "No matching books")
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
