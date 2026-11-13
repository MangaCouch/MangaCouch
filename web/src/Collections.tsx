import { useViewScroll, useViewState } from "./viewState";
import { RouteLink } from "./navigation";
import { useCallback, useEffect, useRef, useState } from "react";
import { BookOpen, GripVertical, Layers, Plus, X } from "lucide-react";
import { useAction } from "./action";
import { BackButton } from "./navigation";
import type { GuardRef } from "./navigation";
import {
  ApiError,
  UserError,
  errorMessage,
  post,
  put,
  request,
  aborted,
} from "./api";
import { toast } from "./feedback";
import { Modal } from "./Modal";
import { SearchBox } from "./SearchBox";
import { LibraryFilters, initialBrowse, searchOptions } from "./LibraryFilters";
import { Segmented, ActionMenu, SkeletonRows } from "./Controls";
import { BookPills, Pill } from "./Pills";
import { Cover } from "./Cover";
import { useBookTitle } from "./PreferenceContext";
import { books as bookCount } from "./format";
import type {
  Book,
  Collection,
  CollectionContext,
  CollectionPage,
  Translate,
} from "./types";

/** Items per collection page request (the server's maximum). */
const ITEM_PAGE = 200;
const itemsPath = (id: string, page: CollectionPage | null) => {
  const params = new URLSearchParams({ limit: String(ITEM_PAGE) });
  if (page?.after) {
    params.set("after", page.after);
    params.set("expected_revision", String(page.revision));
    if (page.result_token) params.set("expected_result", page.result_token);
  }
  return `/api/collections/${id}?${params}`;
};
/** The collection changed since this page loaded it (another tab or device). */
const conflict = (error: unknown) =>
  error instanceof ApiError &&
  error.status === 409 &&
  error.code !== "content_changed";
/** A collection mutation path, checked against the revision the page was loaded at. */
const revised = (path: string, revision: number) =>
  `${path}?expected_revision=${revision}`;

export function Collections({
  id,
  t,
  navigate,
  guard,
}: {
  id?: string;
  t: Translate;
  navigate: (route: string) => void;
  guard: GuardRef;
}) {
  const title = useBookTitle();
  const [items, setItems] = useState<Collection[] | null>(null);
  const [collection, setCollection] = useState<CollectionPage | null>(null);
  const [context, setContext] = useState<CollectionContext | null>(null);
  /** Failure to load the page; action failures live in `action`. */
  const [error, setError] = useState<unknown>(null);
  /** Failure of the latest "add books" search. */
  const [searchError, setSearchError] = useState<unknown>(null);
  const action = useAction();
  const busy = action.busy;
  const [editingRule, setEditingRule] = useState(false);
  const [searchAfter, setSearchAfter] = useState<string | null>(null);
  const [creating, setCreating] = useState(false),
    [deleting, setDeleting] = useState(false),
    [editing, setEditing] = useState(false);
  // A reload (e.g. after a conflict) leaves a name being edited alone.
  const renaming = useRef(false);
  renaming.current = editing;
  const [name, setName] = useState(""),
    [kind, setKind] = useState("static");
  const [filter, setFilter] = useViewState("collections:filter", "static");
  const [kept, setKept] = useViewState(`collection:${id}:items`, ITEM_PAGE);
  const keptRef = useRef(kept);
  keptRef.current = kept;
  useViewScroll(`collection:${id ?? "index"}`, !!(id ? collection : items));
  const [browse, setBrowse] = useState(initialBrowse),
    [query, setQuery] = useState(""),
    [results, setResults] = useState<Book[]>([]);
  // Escape ends renaming; the blur that follows must not save the abandoned name.
  useEffect(() => {
    if (collection) setKept(collection.items.length || ITEM_PAGE);
  }, [collection, setKept]);
  const latestSearch = useRef({ query, shown: results.length > 0 });
  latestSearch.current = { query, shown: results.length > 0 };
  const renameCancelled = useRef(false);
  // Mouse reordering uses native drag and drop; `target` is only for touch drags.
  const drag = useRef<string | null>(null),
    target = useRef<string | null>(null);
  /** Loads the collection with at least `keep` items (what was already shown). */
  const load = useCallback(
    async (signal?: AbortSignal, keep = keptRef.current) => {
      if (!signal?.aborted) setError(null);
      if (id) {
        const [first, state] = await Promise.all([
          request<CollectionPage>(itemsPath(id, null), { signal }),
          request<CollectionContext>(`/api/collections/${id}/context`, {
            signal,
          }),
        ]);
        let value = first;
        while (value.items.length < keep && value.after !== null) {
          const more = await request<CollectionPage>(itemsPath(id, value), {
            signal,
          });
          value = { ...more, items: [...value.items, ...more.items] };
        }
        if (signal?.aborted) return;
        setCollection(value);
        setKept(value.items.length || ITEM_PAGE);
        setContext(state);
        if (!renaming.current) setName(value.name);
      } else {
        let after = "";
        const all: Collection[] = [];
        do {
          const page = await request<{
            items: Collection[];
            after: string | null;
          }>(
            `/api/collections?limit=${ITEM_PAGE}&after=${encodeURIComponent(after)}`,
            { signal },
          );
          all.push(...page.items);
          after = page.after ?? "";
        } while (after);
        if (!signal?.aborted) setItems(all);
      }
    },
    [id, setKept],
  );
  /** Reloads after a change, keeping as many items as were loaded. */
  const reload = async () => {
    try {
      await load(undefined, collection?.items.length ?? 0);
    } catch (error) {
      if (!conflict(error)) throw error;
      // A successful mutation can race with the following paged read. A fresh first
      // page is consistent and leaves further pages available without repeating the write.
      await load(undefined, 0);
    }
  };
  useEffect(() => {
    const controller = new AbortController();
    void load(controller.signal).catch((error) => {
      if (!aborted(error)) setError(error);
    });
    return () => controller.abort();
  }, [load]);
  useEffect(() => {
    guard.current = async () => !busy;
    return () => {
      guard.current = null;
    };
  }, [busy, guard]);
  const searchSerial = useRef(0);
  const act = action.run;
  /**
   * Runs a change checked against the loaded revision. When the collection changed
   * elsewhere, the latest version is loaded at once (a draft name stays) and the person
   * is asked to repeat the change, instead of every retry failing on the old revision.
   */
  const mutate = (task: () => Promise<void>) =>
    act(async () => {
      try {
        await task();
      } catch (error) {
        if (!conflict(error)) throw error;
        await reload();
        if (latestSearch.current.shown)
          await search(latestSearch.current.query);
        throw new UserError(
          t(
            "另一设备已修改这个合集。已载入最新内容，请再操作一次。",
            "Another device changed this collection. The latest version is loaded; please try again.",
          ),
        );
      }
    });
  const openDelete = () => {
    action.setError(null);
    setDeleting(true);
  };
  const startRename = () => {
    renameCancelled.current = false;
    setEditing(true);
  };
  const move = (book: string, before: string | null) => {
    // "Move to the end" is only exact when every item is loaded.
    if (before === null && collection?.after !== null) {
      toast(
        t(
          "请先载入全部书籍，再移动到末尾。",
          "Load all books before moving one to the end.",
        ),
        { id: "collection-move" },
      );
      return;
    }
    return mutate(async () => {
      await put(
        revised(`/api/collections/${id}/items/${book}`, collection!.revision),
        { before_id: before },
      );
      await reload();
    });
  };
  async function search(value: string, after = "") {
    const serial = ++searchSerial.current;
    setQuery(value);
    try {
      const result = await request<{ items: Book[]; after: string | null }>(
        `/api/books?query=${encodeURIComponent(value)}&limit=24&include_total=false&collection_id=${id}&after=${encodeURIComponent(after)}`,
      );
      if (serial !== searchSerial.current) return;
      setResults((old) => (after ? [...old, ...result.items] : result.items));
      setSearchAfter(result.after);
      setSearchError(null);
    } catch (error) {
      // An older search failing late says nothing about the current one.
      if (serial !== searchSerial.current) return;
      setResults([]);
      setSearchError(error);
    }
  }
  async function rename() {
    if (renameCancelled.current) return;
    if (!collection || !name.trim() || name === collection.name) {
      setEditing(false);
      setName(collection?.name ?? "");
      return;
    }
    await mutate(async () => {
      await put(revised(`/api/collections/${id}`, collection.revision), {
        name,
      });
      setEditing(false);
      await reload();
    });
  }
  // The page keeps the browser's own context menu; its actions are on visible buttons.
  return (
    <>
      <main className="content collections-page">
        <BackButton t={t} />
        <header className="section-head">
          {editing ? (
            <input
              className="collection-name-input"
              aria-label={t("合集名称", "Collection name")}
              autoFocus
              maxLength={256}
              value={name}
              onChange={(event) => setName(event.target.value)}
              onBlur={() => void rename()}
              onKeyDown={(event) => {
                if (
                  event.key === "Enter" &&
                  !event.nativeEvent.isComposing &&
                  event.keyCode !== 229
                )
                  void rename();
                if (event.key === "Escape") {
                  renameCancelled.current = true;
                  setEditing(false);
                  setName(collection?.name ?? "");
                }
              }}
            />
          ) : (
            <h1>
              {id ? (
                <button
                  type="button"
                  className="title-edit"
                  title={t("编辑合集名称", "Rename collection")}
                  onClick={startRename}
                >
                  {collection?.name ?? t("加载中…", "Loading…")}
                </button>
              ) : (
                t("合集", "Collections")
              )}
            </h1>
          )}
          {!id ? (
            <button
              type="button"
              className="primary"
              onClick={() => {
                setName("");
                action.setError(null);
                setCreating(true);
              }}
            >
              <Plus size={18} />
              {t("新建合集", "New collection")}
            </button>
          ) : (
            collection && (
              <ActionMenu
                label={t("合集操作", "Collection actions")}
                actions={[
                  ...(collection.kind === "dynamic"
                    ? [
                        {
                          label: t(
                            "转换成静态合集",
                            "Convert to static collection",
                          ),
                          action: () =>
                            void mutate(async () => {
                              await post(
                                revised(
                                  `/api/collections/${id}/freeze`,
                                  collection.revision,
                                ),
                              );
                              await reload();
                            }),
                        },
                      ]
                    : []),
                  {
                    label: t("删除合集", "Delete collection"),
                    danger: true,
                    action: openDelete,
                  },
                ]}
              />
            )
          )}
        </header>
        {!!(error || (!creating && !deleting && action.error)) && (
          <p className="notice error" role="alert">
            {errorMessage(error || action.error, t)}
            {!!error && (
              <button onClick={() => void reload().catch(setError)}>
                {t("重试", "Retry")}
              </button>
            )}
          </p>
        )}
        {!id ? (
          <>
            <Segmented
              label={t("合集类型", "Collection type")}
              value={filter}
              change={setFilter}
              options={[
                { value: "static", label: t("静态合集", "Static") },
                { value: "dynamic", label: t("动态合集", "Dynamic") },
              ]}
            />
            {items === null ? (
              !error && (
                <SkeletonRows
                  label={t("正在载入合集…", "Loading collections…")}
                />
              )
            ) : items.some((item) => item.kind === filter) ? (
              <div className="collection-list">
                {items
                  .filter((item) => item.kind === filter)
                  .map((item) =>
                    item.error ? (
                      <div className="panel" key={item.id}>
                        <strong>{item.name}</strong>
                        <Pill>{t("规则无效", "Invalid rule")}</Pill>
                        <button
                          type="button"
                          className="danger"
                          disabled={busy}
                          onClick={() =>
                            void mutate(async () => {
                              await request(
                                revised(
                                  `/api/collections/${item.id}`,
                                  item.revision,
                                ),
                                { method: "DELETE" },
                              );
                              await reload();
                            })
                          }
                        >
                          {t("删除合集", "Delete collection")}
                        </button>
                      </div>
                    ) : (
                      <RouteLink
                        key={item.id}
                        className="panel"
                        route={`collection/${item.id}`}
                        navigate={navigate}
                      >
                        <strong>{item.name}</strong>
                        <Pill>
                          {item.error
                            ? t("规则无效", "Invalid rule")
                            : t(`${item.count}本`, bookCount(item.count))}
                        </Pill>
                      </RouteLink>
                    ),
                  )}
              </div>
            ) : (
              <div className="empty">
                <Layers size={36} aria-hidden="true" />
                <h2>
                  {filter === "static"
                    ? t("还没有静态合集", "No static collections yet")
                    : t("还没有动态合集", "No dynamic collections yet")}
                </h2>
                <p className="muted">
                  {filter === "static"
                    ? t(
                        "新建一个合集，再从书库或合集页加入书籍。",
                        "Create one, then add books from the library or here.",
                      )
                    : t(
                        "动态合集按搜索和筛选条件自动收录书籍。",
                        "A dynamic collection gathers books matching a search and filters.",
                      )}
                </p>
              </div>
            )}
          </>
        ) : (
          collection && (
            <>
              {collection.kind === "dynamic" && collection.rule && (
                <section className="panel">
                  <h2>{t("动态规则", "Dynamic rule")}</h2>
                  <p>{collection.rule.query || t("全部书籍", "All books")}</p>
                  <p className="muted small">
                    {Object.entries(collection.rule.options)
                      .filter(
                        ([, value]) =>
                          value !== "" &&
                          value !== null &&
                          value !== "all" &&
                          value !== "default",
                      )
                      .map(([key, value]) => {
                        const labels: Record<string, string> = {
                          work_type: t("类型", "Category"),
                          language: t("语言", "Language"),
                          source: t("来源", "Source"),
                          reading: t("阅读状态", "Reading status"),
                          rating: t("我的评分", "My rating"),
                          min_rating: t("最低评分", "Minimum rating"),
                          source_rating_min: t(
                            "最低来源评分",
                            "Minimum source rating",
                          ),
                          order: t("排序", "Order"),
                        };
                        const values: Record<string, string> = {
                          unread: t("未读", "Unread"),
                          in_progress: t("阅读中", "Reading"),
                          completed: t("已读完", "Finished"),
                          rated: t("已评分", "Rated"),
                          unrated: t("未评分", "Unrated"),
                          random: t("随机", "Random"),
                        };
                        return labels[key]
                          ? `${labels[key]}: ${values[String(value)] ?? value}`
                          : "";
                      })
                      .filter(Boolean)
                      .join(" · ")}
                  </p>
                  <button
                    onClick={() => {
                      const options = collection.rule?.options ?? {};
                      setBrowse({
                        ...initialBrowse(),
                        query: collection.rule?.query ?? "",
                        category: String(options.work_type ?? ""),
                        language: String(options.language ?? ""),
                        reading: (options.reading ?? "all") as "all",
                        rating: String(
                          options.min_rating ?? options.rating ?? "all",
                        ),
                        sourceRating: String(
                          options.source_rating_min ?? "all",
                        ),
                      });
                      setKind("dynamic");
                      setEditingRule(true);
                      setCreating(true);
                    }}
                  >
                    {t("编辑规则", "Edit rule")}
                  </button>
                </section>
              )}
              {context?.continue_id && (
                <RouteLink
                  className="primary"
                  navigate={navigate}
                  route={`read/${context.continue_id}/in/${id}${context.all_completed ? "/page/1" : ""}`}
                >
                  <BookOpen size={18} />
                  {context.all_completed
                    ? t("从头再读", "Read again")
                    : context.continue_state === "reading"
                      ? t("继续阅读", "Continue reading")
                      : t("开始阅读", "Start reading")}
                </RouteLink>
              )}
              <ol
                className="collection-items"
                onDragOver={(event) => event.preventDefault()}
              >
                {collection.items.map((item, index) => (
                  <li
                    key={item.book_id}
                    className="collection-row"
                    data-collection-book={item.book_id}
                    onDragOver={(event) => event.preventDefault()}
                    onDrop={(event) => {
                      event.preventDefault();
                      const rect = event.currentTarget.getBoundingClientRect();
                      const before =
                        event.clientY > rect.top + rect.height / 2
                          ? (collection.items[index + 1]?.book_id ?? null)
                          : item.book_id;
                      if (drag.current && drag.current !== before)
                        void move(drag.current, before);
                      drag.current = null;
                    }}
                  >
                    {collection.kind !== "dynamic" && (
                      <button
                        type="button"
                        className="icon-button drag-handle"
                        style={{ touchAction: "none" }}
                        onPointerCancel={() => {
                          drag.current = null;
                          target.current = null;
                        }}
                        onLostPointerCapture={() => {
                          drag.current = null;
                          target.current = null;
                        }}
                        aria-label={t(
                          `拖动排序 ${title(item.book)}`,
                          `Reorder ${title(item.book)}`,
                        )}
                        draggable
                        onDragStart={(event) => {
                          drag.current = item.book_id;
                          event.dataTransfer.effectAllowed = "move";
                        }}
                        onPointerDown={(event) => {
                          if (event.pointerType !== "touch") return;
                          drag.current = item.book_id;
                          target.current = null;
                          event.currentTarget.setPointerCapture(
                            event.pointerId,
                          );
                        }}
                        onPointerMove={(event) => {
                          if (event.pointerType !== "touch" || !drag.current)
                            return;
                          const row = document
                            .elementFromPoint(event.clientX, event.clientY)
                            ?.closest<HTMLElement>("[data-collection-book]");
                          target.current = row?.dataset.collectionBook ?? null;
                          const bounds = row?.getBoundingClientRect();
                          if (
                            row &&
                            bounds &&
                            event.clientY > bounds.top + bounds.height / 2
                          ) {
                            const position = collection.items.findIndex(
                              (i) => i.book_id === row.dataset.collectionBook,
                            );
                            target.current =
                              collection.items[position + 1]?.book_id ??
                              "__end__";
                          }
                        }}
                        onPointerUp={(event) => {
                          if (event.pointerType !== "touch") return;
                          if (
                            drag.current &&
                            target.current &&
                            drag.current !== target.current
                          )
                            void move(
                              drag.current,
                              target.current === "__end__"
                                ? null
                                : target.current,
                            );
                          drag.current = null;
                        }}
                        onKeyDown={(event) => {
                          if (event.key === "ArrowUp" && index > 0) {
                            event.preventDefault();
                            void move(
                              item.book_id,
                              collection.items[index - 1]!.book_id,
                            );
                          }
                          if (
                            event.key === "ArrowDown" &&
                            (index < collection.items.length - 1 ||
                              collection.after !== null)
                          ) {
                            event.preventDefault();
                            void move(
                              item.book_id,
                              collection.items[index + 2]?.book_id ?? null,
                            );
                          }
                        }}
                      >
                        <GripVertical size={18} />
                      </button>
                    )}
                    <RouteLink
                      className="collection-book"
                      route={`book/${item.book_id}/in/${id}`}
                      navigate={navigate}
                    >
                      <Cover
                        id={item.book_id}
                        sha256={item.book.content.sha256}
                        revision={item.book.thumbnail_revision}
                      />
                      <span>
                        <strong>{title(item.book)}</strong>
                        <BookPills book={item.book} t={t} />
                      </span>
                    </RouteLink>
                    {collection.kind !== "dynamic" && (
                      <button
                        type="button"
                        className="icon-button close-button danger"
                        disabled={busy}
                        aria-label={t("移出合集", "Remove from collection")}
                        onClick={() =>
                          void mutate(async () => {
                            await request(
                              revised(
                                `/api/collections/${id}/items/${item.book_id}`,
                                collection.revision,
                              ),
                              { method: "DELETE" },
                            );
                            await reload();
                          })
                        }
                      >
                        <X size={14} />
                      </button>
                    )}
                  </li>
                ))}
              </ol>
              {collection.after !== null && (
                <button
                  type="button"
                  disabled={busy}
                  onClick={() =>
                    void act(async () => {
                      let more: CollectionPage;
                      try {
                        more = await request<CollectionPage>(
                          itemsPath(id!, collection),
                        );
                      } catch (error) {
                        if (!conflict(error)) throw error;
                        // The next page would not follow the one shown: start over.
                        await load();
                        throw new UserError(
                          t(
                            "合集内容已变化，已从第一页重新载入。",
                            "The collection changed, so it was loaded again from the first page.",
                          ),
                        );
                      }
                      setCollection({
                        ...more,
                        items: [...collection.items, ...more.items],
                      });
                    })
                  }
                >
                  {t("更多书籍", "More books")}
                </button>
              )}
              {!collection.count && (
                <p className="empty">
                  {t("这个合集还没有书。", "This collection is empty.")}
                </p>
              )}
              {collection.kind !== "dynamic" && (
                <section className="panel">
                  <h2>{t("加入书籍", "Add books")}</h2>
                  <SearchBox
                    value={query}
                    change={setQuery}
                    search={(value) => void search(value)}
                    t={t}
                  />
                  {!!searchError && (
                    <p className="notice error" role="alert">
                      {errorMessage(searchError, t)}{" "}
                      <button type="button" onClick={() => void search(query)}>
                        {t("重试", "Retry")}
                      </button>
                    </p>
                  )}
                  {results.map((book) => (
                    <div className="collection-search-row" key={book.id}>
                      <span>{title(book)}</span>
                      <button
                        type="button"
                        disabled={busy || !!book.in_collection}
                        onClick={() =>
                          void mutate(async () => {
                            await post(
                              revised(
                                `/api/collections/${id}/items`,
                                collection.revision,
                              ),
                              { book_id: book.id },
                            );
                            await reload();
                            setResults((old) =>
                              old.map((item) =>
                                item.id === book.id
                                  ? { ...item, in_collection: true }
                                  : item,
                              ),
                            );
                          })
                        }
                      >
                        {book.in_collection
                          ? t("已加入", "Added")
                          : t("加入", "Add")}
                      </button>
                    </div>
                  ))}
                  {searchAfter && (
                    <button
                      disabled={busy}
                      onClick={() => void search(query, searchAfter)}
                    >
                      {t("更多搜索结果", "More search results")}
                    </button>
                  )}
                </section>
              )}
            </>
          )
        )}
        {creating && (
          <Modal
            title={
              editingRule
                ? t("编辑规则", "Edit rule")
                : t("新建合集", "New collection")
            }
            closeLabel={t("关闭", "Close")}
            close={() => {
              setCreating(false);
              setEditingRule(false);
            }}
            busy={busy}
          >
            <form
              className="modal-form"
              onSubmit={(event) => {
                event.preventDefault();
                void mutate(async () => {
                  if (editingRule && collection) {
                    await put(
                      revised(
                        `/api/collections/${id}/rule`,
                        collection.revision,
                      ),
                      {
                        rule: {
                          query: browse.query,
                          options: searchOptions(browse),
                        },
                      },
                    );
                    setCreating(false);
                    setEditingRule(false);
                    await reload();
                    return;
                  }
                  const value = await post<Collection>("/api/collections", {
                    name,
                    ...(kind === "dynamic"
                      ? {
                          rule: {
                            query: browse.query,
                            options: searchOptions(browse),
                          },
                        }
                      : {}),
                  });
                  setCreating(false);
                  setName("");
                  await load();
                  guard.current = null;
                  navigate(`collection/${value.id}`);
                });
              }}
            >
              {!!action.error && (
                <p className="notice error" role="alert">
                  {errorMessage(action.error, t)}
                </p>
              )}
              <label>
                {t("名称", "Name")}
                <input
                  value={name}
                  disabled={editingRule}
                  required
                  autoFocus
                  maxLength={256}
                  onChange={(event) => setName(event.target.value)}
                />
              </label>
              {!editingRule && (
                <Segmented
                  label={t("合集类型", "Collection type")}
                  value={kind}
                  change={setKind}
                  options={[
                    { value: "static", label: t("静态", "Static") },
                    { value: "dynamic", label: t("动态", "Dynamic") },
                  ]}
                />
              )}
              {kind === "dynamic" && (
                <div className="collection-rule">
                  <SearchBox
                    embedded
                    value={browse.query}
                    change={(query) => setBrowse({ ...browse, query })}
                    search={(query) => setBrowse({ ...browse, query })}
                    t={t}
                  />
                  <LibraryFilters value={browse} change={setBrowse} t={t} />
                </div>
              )}
              <div className="actions modal-actions">
                <button
                  type="button"
                  disabled={busy}
                  onClick={() => {
                    setCreating(false);
                    setEditingRule(false);
                  }}
                >
                  {t("取消", "Cancel")}
                </button>
                <button
                  type="submit"
                  className="primary"
                  disabled={busy || !name.trim()}
                >
                  {editingRule
                    ? t("保存规则", "Save rule")
                    : t("创建", "Create")}
                </button>
              </div>
            </form>
          </Modal>
        )}
        {deleting && collection && (
          <Modal
            title={t(
              `删除合集 ${collection.name}？`,
              `Delete ${collection.name}?`,
            )}
            closeLabel={t("取消", "Cancel")}
            close={() => setDeleting(false)}
            busy={busy}
            dismissOutside={false}
          >
            {!!action.error && (
              <p className="notice error" role="alert">
                {errorMessage(action.error, t)}
              </p>
            )}
            <div className="actions modal-actions">
              <button type="button" onClick={() => setDeleting(false)}>
                {t("取消", "Cancel")}
              </button>
              <button
                type="button"
                className="danger"
                disabled={busy}
                onClick={() =>
                  void mutate(async () => {
                    await request(
                      revised(`/api/collections/${id}`, collection.revision),
                      { method: "DELETE" },
                    );
                    setDeleting(false);
                    guard.current = null;
                    navigate("collections");
                  })
                }
              >
                {t("确认删除", "Delete")}
              </button>
            </div>
          </Modal>
        )}
      </main>
    </>
  );
}
