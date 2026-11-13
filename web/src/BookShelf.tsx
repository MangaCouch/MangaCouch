import { readingStatus } from "./reading";
import { BookPills, Pill } from "./Pills";
import { ContextActions } from "./Controls";
import type { MenuEntry } from "./Controls";
import { useEffect, useId, useRef, useState } from "react";
import { BookOpen, Book as BookIcon, Grid2X2, List, X } from "lucide-react";
import type { BookTargets } from "./bookActions";
import { Cover } from "./Cover";
import { preference, remember } from "./device";
import { useBookTitle } from "./PreferenceContext";
import { post } from "./api";
import { searchOptions } from "./LibraryFilters";
import { toast } from "./feedback";
import type { Browse } from "./LibraryFilters";
import type { Book, Translate } from "./types";

/**
 * The shelf's selection, kept by the library so it carries across pages: the chosen
 * books, or (`all`) every search result except `excluded`. Empty means not selecting.
 */
type ShelfSelection = {
  picked: Set<string>;
  all: boolean;
  excluded: Set<string>;
};
export const noSelection = (): ShelfSelection => ({
  picked: new Set(),
  all: false,
  excluded: new Set(),
});
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
const PLACEHOLDERS = 12;
/** The most results the server acts on as "every result" in one request. */
const MAX_ALL = 100_000;
/** The most books "every result" can leave out. */
const MAX_EXCLUDED = 500;
/** Ctrl/⌘-click and middle-click open the book in a new tab, as they would for a link. */
function openInNewTab(id: string) {
  window.open(
    `${location.pathname}${location.search}#book/${id}`,
    "_blank",
    "noopener",
  );
}
export function BookShelf({
  books,
  total,
  browse,
  busy,
  loading = false,
  t,
  open,
  selection,
  select: setSelection,
  menu,
}: {
  books: Book[];
  total: number | null;
  browse: Browse;
  busy: boolean;
  loading?: boolean;
  t: Translate;
  open: (id: string) => void;
  selection: ShelfSelection;
  select: (selection: ShelfSelection) => void;
  /** The book menu for a book or the selection. */
  menu: (targets: BookTargets) => MenuEntry[];
}) {
  const [menuState, setMenuState] = useState<boolean | null>(null);
  const menuRequest = useRef(0);
  async function inspectSelection(book: Book) {
    const generation = ++menuRequest.current;
    setMenuState(null);
    const target = targetsFor(book);
    try {
      let unfinished = false;
      const requests = target.all
        ? [
            {
              query: target.all.browse.query,
              options: searchOptions(target.all.browse),
              exclude: target.all.exclude,
            },
          ]
        : Array.from(
            { length: Math.ceil(target.bookIds.length / 500) },
            (_, n) => ({
              book_ids: target.bookIds.slice(n * 500, (n + 1) * 500),
            }),
          );
      for (const body of requests) {
        const result = await post<{ unfinished: boolean }>(
          "/api/books/selection-state",
          body,
        );
        unfinished ||= result.unfinished;
      }
      if (generation === menuRequest.current) setMenuState(unfinished);
    } catch {
      /* The disabled loading item prevents a stale action; reopening retries. */
    }
  }
  const title = useBookTitle();
  const facts = useId();
  const [view, setView] = useState(() =>
    preference("shelf-view", "grid") === "list" ? "list" : "grid",
  );
  const shownTotal = loading ? "…" : (total ?? "…");
  const { picked, all, excluded } = selection;
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
  const count = all ? Math.max(0, (total ?? 0) - excluded.size) : picked.size;
  // Choosing a book starts selecting; un-choosing the last one ends it.
  const mode = count > 0;
  const checked = (id: string) => (all ? !excluded.has(id) : picked.has(id));
  const byId = new Map(books.map((book) => [book.id, book]));
  /** Replaces the selection; one that no longer holds a book ends selecting. */
  function update(next: ShelfSelection) {
    const empty = next.all
      ? next.excluded.size >= (total ?? 0)
      : next.picked.size === 0;
    setSelection(empty ? noSelection() : next);
    if (empty) anchor.current = null;
  }
  function clear() {
    update(noSelection());
  }
  /** "Select all": this page first, then (pressed again) every result. */
  function selectAll() {
    if (books.every((book) => picked.has(book.id))) {
      if ((total ?? 0) > MAX_ALL) {
        toast.warning(
          t(
            `一次最多可选 ${MAX_ALL.toLocaleString("zh-CN")} 本，请先缩小搜索或筛选范围。`,
            `At most ${MAX_ALL.toLocaleString("en-US")} books can be selected at once. Narrow the search or filters first.`,
          ),
          { id: "select-all-limit" },
        );
        return;
      }
      update({ picked: new Set(), all: true, excluded: new Set() });
    } else
      update({
        ...selection,
        picked: new Set([...picked, ...books.map((b) => b.id)]),
      });
  }
  function select(id: string, shift = false) {
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
      if (next.size > MAX_EXCLUDED) {
        toast.warning(
          t(
            `全选时最多可取消 ${MAX_EXCLUDED} 本；需要排除更多时，请缩小搜索范围或逐本选择。`,
            `With all results selected, at most ${MAX_EXCLUDED} books can be left out. Narrow the search or pick books one by one instead.`,
          ),
          { id: "select-all-limit" },
        );
        return;
      }
      update({ ...selection, excluded: next });
    } else {
      const next = new Set(picked);
      for (const key of ids) {
        const book = byId.get(key);
        if (book && (shift || !next.has(key))) next.add(key);
        else next.delete(key);
      }
      update({ ...selection, picked: next });
    }
    if (!shift) anchor.current = id;
  }
  /** What the menu of a book acts on: the selection when the book is in it, else the book. */
  function targetsFor(book: Book): BookTargets {
    if (!mode || !checked(book.id))
      return {
        bookIds: [book.id],
        anchor: book,
        unfinished: menuState,
      };
    const chosen = [...picked];
    return all && total !== null
      ? {
          bookIds: chosen,
          all: { browse, total, exclude: [...excluded] },
          anchor: book,
          unfinished: menuState,
        }
      : {
          bookIds: chosen,
          anchor: book,
          unfinished: menuState,
        };
  }
  function finishDrag() {
    drag.current = null;
    setRect(null);
  }
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
      // An open filter popover takes Escape for itself.
      if (
        event.key === "Escape" &&
        mode &&
        !document.querySelector("details.library-filters[open]")
      ) {
        event.preventDefault();
        clear();
      }
      if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === "a") {
        event.preventDefault();
        selectAll();
      }
    };
    document.addEventListener("keydown", keyboard);
    return () => {
      document.removeEventListener("keydown", keyboard);
    };
  });
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
            <Pill>{t(`${shownTotal} 本`, `${shownTotal} books`)}</Pill>
          </small>
        </div>
        {mode && (
          <div
            className="selection-toolbar"
            aria-label={t("批量选择", "Bulk selection")}
          >
            <button
              type="button"
              className="icon-button close-button"
              aria-label={t("清空选择", "Clear selection")}
              onClick={clear}
            >
              <X size={14} />
            </button>
            <strong role="status">
              {all
                ? t(
                    `已选全部结果中的 ${count} 本`,
                    `${count} selected (all results)`,
                  )
                : t(`已选 ${count} 本`, `${count} selected`)}
            </strong>
            {!all && count < (total ?? 0) && (
              <button type="button" disabled={busy} onClick={selectAll}>
                {t("全选", "Select all")}
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
        </div>
      </div>
      <div
        ref={root}
        className={`book-grid selectable-shelf ${view === "list" ? "compact-shelf" : ""}`}
        aria-label={t("书籍", "Books")}
        aria-busy={loading}
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
            base: additive ? new Set(picked) : new Set(),
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
              ) {
                const book = byId.get(node.dataset.bookId!);
                if (book) next.add(book.id);
              }
            });
          update({ ...selection, picked: next });
          if (event.clientY > window.innerHeight - 45) window.scrollBy(0, 18);
          else if (event.clientY < 45) window.scrollBy(0, -18);
        }}
        onPointerUp={finishDrag}
        onPointerCancel={finishDrag}
        onLostPointerCapture={finishDrag}
      >
        {loading &&
          Array.from({ length: PLACEHOLDERS }, (_, index) => (
            <div className="shelf-item skeleton" key={index} aria-hidden="true">
              <div className="book-card">
                <div className="cover skeleton-block" />
                <span className="skeleton-line" />
                <span className="skeleton-line short" />
              </div>
            </div>
          ))}
        {!loading &&
          books.map((book, index) => (
            <ContextActions
              key={book.id}
              opened={() => void inspectSelection(book)}
              actions={() => menu(targetsFor(book))}
            >
              <article
                className={`shelf-item ${checked(book.id) ? "selected" : ""}`}
                data-book-id={book.id}
                key={book.id}
                onKeyDown={(event) => {
                  if (
                    !(event.shiftKey && event.key === "F10") &&
                    event.key !== "ContextMenu"
                  )
                    return;
                  event.preventDefault();
                  const box = event.currentTarget.getBoundingClientRect();
                  event.currentTarget.dispatchEvent(
                    new MouseEvent("contextmenu", {
                      bubbles: true,
                      cancelable: true,
                      clientX: box.left + box.width / 2,
                      clientY: box.top + box.height / 2,
                    }),
                  );
                }}
              >
                <button
                  type="button"
                  className="book-select"
                  aria-label={t(`选择 ${title(book)}`, `Select ${title(book)}`)}
                  aria-pressed={checked(book.id)}
                  aria-keyshortcuts="Shift+F10 ContextMenu"
                  onClick={(event) => select(book.id, event.shiftKey)}
                >
                  <span aria-hidden="true">{checked(book.id) ? "✓" : ""}</span>
                </button>
                <button
                  type="button"
                  className="book-card"
                  aria-label={title(book)}
                  aria-describedby={`${facts}-${book.id}`}
                  aria-keyshortcuts="Shift+F10 ContextMenu"
                  onClick={(event) => {
                    if (event.ctrlKey || event.metaKey) openInNewTab(book.id);
                    else if (mode || event.shiftKey)
                      select(book.id, event.shiftKey);
                    else open(book.id);
                  }}
                  onAuxClick={(event) => {
                    if (event.button === 1) openInNewTab(book.id);
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
                    <Cover
                      id={book.id}
                      sha256={book.content.sha256}
                      revision={book.thumbnail_revision}
                    />
                    {view === "grid" &&
                      readingStatus(book.personal) !== "finished" && (
                        <span className="cover-badge">
                          {readingStatus(book.personal) === "unread" ? (
                            <BookIcon
                              size={16}
                              aria-label={t("未读", "Unread")}
                            />
                          ) : (
                            <BookOpen
                              size={16}
                              aria-label={t("阅读中", "Reading")}
                            />
                          )}
                        </span>
                      )}
                  </div>
                  <strong>{title(book)}</strong>
                  <span className="card-facts" id={`${facts}-${book.id}`}>
                    <BookPills book={book} t={t} />
                    {book.review_state !== "confirmed" && (
                      <span className="review-dot">
                        {t("需核对", "Needs review")}
                      </span>
                    )}
                  </span>
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
