import { BookPills, Pill } from "./Pills";
import { ContextActions } from "./Controls";
import { useEffect, useRef, useState } from "react";
import {
  BookOpen,
  Book as BookIcon,
  CheckSquare2,
  Grid2X2,
  List,
} from "lucide-react";
import { Cover } from "./Cover";
import { preference, remember } from "./device";
import { useBookTitle } from "./PreferenceContext";
import type { Browse } from "./LibraryFilters";
import type { Book, Translate } from "./types";

export type LibrarySelection = {
  books: Book[];
  all?: { browse: Browse; total: number; exclude: string[] };
};
export function rangeIds(ids: string[], anchor: string | null, target: string) {
  const end = ids.indexOf(target),
    start = anchor ? ids.indexOf(anchor) : 0;
  return end < 0
    ? []
    : ids.slice(
        Math.min(Math.max(0, start), end),
        Math.max(Math.max(0, start), end) + 1,
      );
}
export function intersects(
  a: { left: number; top: number; right: number; bottom: number },
  b: typeof a,
) {
  return (
    a.left <= b.right &&
    a.right >= b.left &&
    a.top <= b.bottom &&
    a.bottom >= b.top
  );
}
export function BookShelf({
  books,
  total,
  browse,
  busy,
  t,
  open,
  organize,
  selectRequested,
  onSelectionStarted,
}: {
  books: Book[];
  total: number | null;
  browse: Browse;
  busy: boolean;
  t: Translate;
  open: (id: string) => void;
  organize: (selection: LibrarySelection) => void;
  selectRequested: boolean;
  onSelectionStarted: () => void;
}) {
  const title = useBookTitle();
  const [view, setView] = useState(() =>
    preference("shelf-view", "grid") === "list" ? "list" : "grid",
  );
  const [mode, setMode] = useState(false);
  const [selected, setSelected] = useState<Set<string>>(new Set());
  const [all, setAll] = useState(false);
  const [excluded, setExcluded] = useState<Set<string>>(new Set());
  const anchor = useRef<string | null>(null);
  const root = useRef<HTMLDivElement>(null);
  const drag = useRef<{
    x: number;
    y: number;
    base: Set<string>;
    started: boolean;
  } | null>(null);
  const suppressClick = useRef(false);
  const [rect, setRect] = useState<{
    left: number;
    top: number;
    width: number;
    height: number;
  } | null>(null);
  const count = all ? Math.max(0, (total ?? 0) - excluded.size) : selected.size;
  const checked = (id: string) => (all ? !excluded.has(id) : selected.has(id));
  function clear() {
    setSelected(new Set());
    setExcluded(new Set());
    setAll(false);
    anchor.current = null;
  }
  function select(id: string, shift = false) {
    setMode(true);
    const ids = shift
      ? rangeIds(
          books.map((book) => book.id),
          anchor.current,
          id,
        )
      : [id];
    if (all) {
      const next = new Set(excluded);
      for (const key of ids) {
        if (shift || next.has(key)) next.delete(key);
        else next.add(key);
      }
      setExcluded(next);
    } else {
      const next = new Set(selected);
      for (const key of ids) {
        if (shift || !next.has(key)) next.add(key);
        else next.delete(key);
      }
      setSelected(next);
    }
    if (!shift) anchor.current = id;
  }
  function finishDrag() {
    drag.current = null;
    setRect(null);
  }
  useEffect(() => {
    if (selectRequested) {
      setMode(true);
      onSelectionStarted();
    }
  }, [selectRequested, onSelectionStarted]);
  useEffect(() => {
    const keyboard = (event: KeyboardEvent) => {
      if (
        event.defaultPrevented ||
        document.querySelector("dialog[open],[role=dialog],[role=menu]")
      )
        return;
      if (
        (event.target as HTMLElement).closest?.(
          "input,textarea,select,[contenteditable=true]",
        )
      )
        return;
      if (event.key === "Escape" && (mode || count)) {
        event.preventDefault();
        clear();
        setMode(false);
      }
      if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === "a") {
        event.preventDefault();
        setMode(true);
        setAll(false);
        setExcluded(new Set());
        setSelected(new Set(books.map((book) => book.id)));
      }
    };
    document.addEventListener("keydown", keyboard);
    return () => {
      document.removeEventListener("keydown", keyboard);
    };
  }, [books, mode, count]);
  return (
    <section
      className={`shelf ${mode ? "selection-mode" : ""}`}
      aria-label={t("书架", "Bookshelf")}
    >
      <div className="shelf-heading">
        <div>
          <h2>
            {browse.query
              ? t("搜索结果", "Search results")
              : t("我的书架", "My bookshelf")}
          </h2>
          <small className="muted" role="status">
            <Pill>
              {t(`本页 ${books.length} 本`, `${books.length} on page`)}
            </Pill>{" "}
            <Pill>{t(`共 ${total ?? "…"} 本`, `${total ?? "…"} total`)}</Pill>
          </small>
        </div>
        {mode && (
          <div
            className="selection-toolbar"
            aria-label={t("批量选择", "Bulk selection")}
          >
            <strong role="status">
              {all
                ? t(
                    `全部结果中已选 ${count} 本`,
                    `${count} selected across all results`,
                  )
                : t(`已选 ${count} 本`, `${count} selected`)}
            </strong>
            <button
              type="button"
              disabled={!books.length || busy}
              onClick={() => {
                setAll(false);
                setExcluded(new Set());
                setSelected(new Set(books.map((b) => b.id)));
              }}
            >
              {t(
                `选择已加载 ${books.length} 本`,
                `Select ${books.length} loaded`,
              )}
            </button>
            <button
              type="button"
              disabled={!total || busy}
              onClick={() => {
                setAll(true);
                setExcluded(new Set());
                setSelected(new Set());
              }}
            >
              {t(
                `选择全部 ${total ?? "…"} 个结果`,
                `Select all ${total ?? "…"} results`,
              )}
            </button>
            <button
              type="button"
              className="primary"
              disabled={!count || busy}
              onClick={() =>
                organize({
                  books: books.filter((book) => selected.has(book.id)),
                  ...(all && total !== null
                    ? { all: { browse, total, exclude: [...excluded] } }
                    : {}),
                })
              }
            >
              {t("批量操作", "Bulk actions")}
            </button>
            {count > 0 && (
              <button type="button" onClick={clear}>
                {t("清空选择", "Clear selection")}
              </button>
            )}
          </div>
        )}
        <div className="actions view-controls">
          <button
            type="button"
            className="icon-button"
            aria-label={t("封面网格", "Cover grid")}
            aria-pressed={view === "grid"}
            onClick={() => {
              setView("grid");
              remember("shelf-view", "grid");
            }}
          >
            <Grid2X2 size={19} />
          </button>
          <button
            type="button"
            className="icon-button"
            aria-label={t("紧凑列表", "Compact list")}
            aria-pressed={view === "list"}
            onClick={() => {
              setView("list");
              remember("shelf-view", "list");
            }}
          >
            <List size={19} />
          </button>
          <button
            type="button"
            aria-pressed={mode}
            onClick={() => {
              setMode(!mode);
              clear();
            }}
          >
            <CheckSquare2 size={18} />
            {t("选择", "Select")}
            <small className="select-shortcut">Ctrl/⌘</small>
          </button>
        </div>
      </div>
      <div
        ref={root}
        className={`book-grid selectable-shelf ${view === "list" ? "compact-shelf" : ""}`}
        aria-label={t("书籍", "Books")}
        onClickCapture={(event) => {
          if (suppressClick.current) {
            event.preventDefault();
            event.stopPropagation();
            suppressClick.current = false;
          }
        }}
        onDragStart={(event) => event.preventDefault()}
        onPointerDown={(event) => {
          if (
            event.pointerType !== "mouse" ||
            event.button !== 0 ||
            all ||
            (event.target as HTMLElement).closest(".book-card,.book-select")
          )
            return;
          suppressClick.current = false;
          // A modifier key adds the dragged range to the selection; a plain drag replaces it.
          const additive = event.ctrlKey || event.metaKey || event.shiftKey;
          drag.current = {
            x: event.pageX,
            y: event.pageY,
            base: additive ? new Set(selected) : new Set(),
            started: false,
          };
        }}
        onPointerMove={(event) => {
          const current = drag.current;
          if (!current || !root.current || event.buttons !== 1) return;
          if (
            !current.started &&
            Math.hypot(event.pageX - current.x, event.pageY - current.y) < 7
          )
            return;
          current.started = true;
          suppressClick.current = true;
          setMode(true);
          event.preventDefault();
          root.current.setPointerCapture(event.pointerId);
          const box = {
            left: Math.min(current.x, event.pageX),
            top: Math.min(current.y, event.pageY),
            right: Math.max(current.x, event.pageX),
            bottom: Math.max(current.y, event.pageY),
          };
          const bounds = root.current.getBoundingClientRect();
          setRect({
            left: box.left - bounds.left - window.scrollX,
            top: box.top - bounds.top - window.scrollY,
            width: box.right - box.left,
            height: box.bottom - box.top,
          });
          const next = new Set(current.base);
          root.current
            .querySelectorAll<HTMLElement>("[data-book-id]")
            .forEach((node) => {
              const r = node.getBoundingClientRect();
              if (
                intersects(box, {
                  left: r.left + window.scrollX,
                  top: r.top + window.scrollY,
                  right: r.right + window.scrollX,
                  bottom: r.bottom + window.scrollY,
                })
              )
                next.add(node.dataset.bookId!);
            });
          setSelected(next);
          if (event.clientY > window.innerHeight - 45) window.scrollBy(0, 18);
          else if (event.clientY < 45) window.scrollBy(0, -18);
        }}
        onPointerUp={finishDrag}
        onPointerCancel={finishDrag}
        onLostPointerCapture={finishDrag}
      >
        {books.map((book, index) => (
          <ContextActions
            key={book.id}
            actions={[
              {
                label: t("打开书籍", "Open book"),
                action: () => open(book.id),
              },
              { label: t("选择", "Select"), action: () => select(book.id) },
              {
                label: t("批量操作", "Bulk actions"),
                action: () =>
                  organize({
                    books: count
                      ? books.filter((item) => checked(item.id))
                      : [book],
                    ...(all && total !== null
                      ? { all: { browse, total, exclude: [...excluded] } }
                      : {}),
                  }),
              },
            ]}
          >
            <article
              className={`shelf-item ${checked(book.id) ? "selected" : ""}`}
              data-book-id={book.id}
              key={book.id}
            >
              <button
                type="button"
                className="book-select"
                aria-label={t(`选择 ${title(book)}`, `Select ${title(book)}`)}
                aria-pressed={checked(book.id)}
                onClick={(event) => select(book.id, event.shiftKey)}
              >
                <span aria-hidden="true">{checked(book.id) ? "✓" : ""}</span>
              </button>
              <button
                type="button"
                className="book-card"
                aria-label={title(book)}
                onClick={(event) => {
                  if (mode || event.shiftKey || event.ctrlKey || event.metaKey)
                    select(book.id, event.shiftKey);
                  else open(book.id);
                }}
                onKeyDown={(event) => {
                  if ([" ", "Enter"].includes(event.key) && mode) {
                    event.preventDefault();
                    select(book.id, event.shiftKey);
                    return;
                  }
                  const keys = [
                    "ArrowLeft",
                    "ArrowRight",
                    "ArrowUp",
                    "ArrowDown",
                    "Home",
                    "End",
                  ];
                  if (!keys.includes(event.key)) return;
                  event.preventDefault();
                  const nodes = Array.from(
                    root.current?.querySelectorAll<HTMLButtonElement>(
                      ".book-card",
                    ) ?? [],
                  );
                  const columns =
                    view === "list" || nodes.length < 2
                      ? 1
                      : nodes.filter(
                          (node) =>
                            Math.abs(
                              node.getBoundingClientRect().top -
                                nodes[0]!.getBoundingClientRect().top,
                            ) < 2,
                        ).length;
                  const delta =
                    event.key === "ArrowUp"
                      ? -columns
                      : event.key === "ArrowDown"
                        ? columns
                        : event.key === "ArrowLeft"
                          ? -1
                          : 1;
                  const target =
                    event.key === "Home"
                      ? 0
                      : event.key === "End"
                        ? books.length - 1
                        : Math.min(
                            books.length - 1,
                            Math.max(0, index + delta),
                          );
                  if (event.shiftKey) {
                    if (!anchor.current) anchor.current = book.id;
                    select(books[target]!.id, true);
                  }
                  nodes[target]?.focus();
                }}
              >
                <div className="cover">
                  <BookOpen className="cover-fallback" />
                  <Cover id={book.id} sha256={book.content.sha256} />
                  {view === "grid" && !book.personal.completed && (
                    <span className="cover-badge">
                      {book.personal.resume_page === null ? (
                        <BookIcon size={16} aria-label={t("未读", "Unread")} />
                      ) : (
                        <BookOpen
                          size={16}
                          aria-label={t("正在读", "Reading")}
                        />
                      )}
                    </span>
                  )}
                </div>
                <strong>{title(book)}</strong>
                <BookPills book={book} t={t} />
                {book.review_state !== "confirmed" && (
                  <span className="review-dot">
                    {t("待核对", "Needs review")}
                  </span>
                )}
              </button>
            </article>
          </ContextActions>
        ))}
        {rect && (
          <div
            className="selection-rectangle"
            style={rect}
            aria-hidden="true"
          />
        )}
      </div>
    </section>
  );
}
