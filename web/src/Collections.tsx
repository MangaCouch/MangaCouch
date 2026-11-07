import { useCallback, useEffect, useRef, useState } from "react";
import { BookOpen, GripVertical, Plus, X } from "lucide-react";
import { BackButton } from "./navigation";
import { errorMessage, post, put, request, aborted } from "./api";
import { toast } from "./feedback";
import { Modal } from "./Modal";
import { SearchBox } from "./SearchBox";
import {
  LibraryFilters,
  initialBrowse,
  searchParameters,
} from "./LibraryFilters";
import { Segmented, ActionMenu, ContextActions } from "./Controls";
import { BookPills, Pill } from "./Pills";
import { Cover } from "./Cover";
import { useBookTitle } from "./PreferenceContext";
import type {
  Book,
  Collection,
  CollectionContext,
  CollectionPage,
  Translate,
} from "./types";

/** Items per collection page request (the server's maximum). */
const ITEM_PAGE = 200;
const itemsPath = (id: string, page: CollectionPage | null) =>
  page
    ? `/api/collections/${id}?limit=${ITEM_PAGE}&after_position=${page.after_position}&expected_revision=${page.revision}${page.result_token ? `&expected_result=${page.result_token}` : ""}`
    : `/api/collections/${id}?limit=${ITEM_PAGE}`;

export function Collections({
  id,
  t,
  navigate,
  guard,
}: {
  id?: string;
  t: Translate;
  navigate: (route: string) => void;
  guard: { current: (() => Promise<boolean>) | null };
}) {
  const title = useBookTitle();
  const [items, setItems] = useState<Collection[]>([]);
  const [collection, setCollection] = useState<CollectionPage | null>(null);
  const [context, setContext] = useState<CollectionContext | null>(null);
  const [busy, setBusy] = useState(false),
    [error, setError] = useState<unknown>(null);
  const [creating, setCreating] = useState(false),
    [deleting, setDeleting] = useState(false),
    [editing, setEditing] = useState(false);
  const [name, setName] = useState(""),
    [kind, setKind] = useState("static"),
    [filter, setFilter] = useState("static");
  const [browse, setBrowse] = useState(initialBrowse),
    [query, setQuery] = useState(""),
    [results, setResults] = useState<Book[]>([]);
  // Mouse reordering uses native drag and drop; `target` is only for touch drags.
  const drag = useRef<string | null>(null),
    target = useRef<string | null>(null);
  /** Loads the collection with at least `keep` items (what was already shown). */
  const load = useCallback(
    async (signal?: AbortSignal, keep = 0) => {
      if (id) {
        const [first, state] = await Promise.all([
          request<CollectionPage>(itemsPath(id, null), { signal }),
          request<CollectionContext>(`/api/collections/${id}/context`, {
            signal,
          }),
        ]);
        let value = first;
        while (value.items.length < keep && value.after_position !== null) {
          const more = await request<CollectionPage>(itemsPath(id, value), {
            signal,
          });
          value = { ...more, items: [...value.items, ...more.items] };
        }
        if (signal?.aborted) return;
        setCollection(value);
        setContext(state);
        setName(value.name);
      } else {
        let after = "";
        const all: Collection[] = [];
        do {
          const page = await request<{
            collections: Collection[];
            after: string | null;
          }>(
            `/api/collections?limit=${ITEM_PAGE}&after=${encodeURIComponent(after)}`,
            { signal },
          );
          all.push(...page.collections);
          after = page.after ?? "";
        } while (after);
        if (!signal?.aborted) setItems(all);
      }
    },
    [id],
  );
  /** Reloads after a change, keeping as many items as were loaded. */
  const reload = () => load(undefined, collection?.items.length ?? 0);
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
  const working = useRef(false);
  const searchSerial = useRef(0);
  async function act(task: () => Promise<void>) {
    if (working.current) return;
    working.current = true;
    setBusy(true);
    setError(null);
    try {
      await task();
    } catch (error) {
      setError(error);
    } finally {
      working.current = false;
      setBusy(false);
    }
  }
  const move = (book: string, before: string | null) => {
    // "Move to the end" is only exact when every item is loaded.
    if (before === null && collection?.after_position !== null) {
      toast(
        t(
          "请先载入全部书籍，再移动到末尾。",
          "Load all books before moving one to the end.",
        ),
        { id: "collection-move" },
      );
      return;
    }
    return act(async () => {
      await put(`/api/collections/${id}/items/${book}`, {
        expected_revision: collection!.revision,
        before_id: before,
      });
      await reload();
    });
  };
  async function search(value: string) {
    const serial = ++searchSerial.current;
    setQuery(value);
    try {
      const result = await request<{ books: Book[] }>(
        `/api/books?query=${encodeURIComponent(value)}&limit=24`,
      );
      if (serial === searchSerial.current) setResults(result.books);
    } catch (error) {
      setError(error);
    }
  }
  async function rename() {
    if (!collection || !name.trim() || name === collection.name) {
      setEditing(false);
      setName(collection?.name ?? "");
      return;
    }
    await act(async () => {
      await put(`/api/collections/${id}`, {
        name,
        expected_revision: collection.revision,
      });
      setEditing(false);
      await reload();
    });
  }
  return (
    <ContextActions
      actions={[
        { label: t("返回书库", "Library"), action: () => navigate("") },
        ...(id
          ? [
              {
                label: t("编辑合集名称", "Rename collection"),
                action: () => setEditing(true),
              },
              {
                label: t("删除合集", "Delete collection"),
                danger: true,
                action: () => setDeleting(true),
              },
            ]
          : [
              {
                label: t("新建合集", "New collection"),
                action: () => {
                  setName("");
                  setCreating(true);
                },
              },
            ]),
      ]}
    >
      <main className="content collections-page">
        <BackButton t={t} />
        <span className="eyebrow">{t("我的合集", "COLLECTIONS")}</span>
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
                  className="text-button title-edit"
                  onClick={() => setEditing(true)}
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
                            void act(async () => {
                              await post(`/api/collections/${id}/freeze`, {
                                expected_revision: collection.revision,
                              });
                              await reload();
                            }),
                        },
                      ]
                    : []),
                  {
                    label: t("删除合集", "Delete collection"),
                    danger: true,
                    action: () => setDeleting(true),
                  },
                ]}
              />
            )
          )}
        </header>
        {!!error && (
          <p className="notice error" role="alert">
            {errorMessage(error, t)}
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
            <div className="collection-list">
              {items
                .filter((item) => item.kind === filter)
                .map((item) => (
                  <button
                    type="button"
                    key={item.id}
                    className="panel"
                    onClick={() => navigate(`collection/${item.id}`)}
                  >
                    <strong>{item.name}</strong>
                    <Pill>
                      {item.count}
                      {t("本", " books")}
                    </Pill>
                  </button>
                ))}
            </div>
          </>
        ) : (
          collection && (
            <>
              {context?.continue_id && (
                <button
                  type="button"
                  className="primary"
                  onClick={() =>
                    navigate(`read/${context.continue_id}/in/${id}`)
                  }
                >
                  <BookOpen size={18} />
                  {context.all_completed
                    ? t("从头再读", "Read again")
                    : context.continue_state === "reading"
                      ? t("继续阅读", "Continue reading")
                      : t("开始阅读", "Start reading")}
                </button>
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
                              collection.after_position !== null)
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
                    <button
                      type="button"
                      className="collection-book"
                      onClick={() => navigate(`book/${item.book_id}/in/${id}`)}
                    >
                      <Cover
                        id={item.book_id}
                        sha256={item.book.content.sha256}
                      />
                      <span>
                        <strong>{title(item.book)}</strong>
                        <BookPills book={item.book} t={t} />
                      </span>
                    </button>
                    {collection.kind !== "dynamic" && (
                      <button
                        type="button"
                        className="icon-button danger"
                        disabled={busy}
                        aria-label={t("移出合集", "Remove from collection")}
                        onClick={() =>
                          void act(async () => {
                            await request(
                              `/api/collections/${id}/items/${item.book_id}?expected_revision=${collection.revision}`,
                              { method: "DELETE" },
                            );
                            await reload();
                          })
                        }
                      >
                        <X size={17} />
                      </button>
                    )}
                  </li>
                ))}
              </ol>
              {collection.after_position !== null && (
                <button
                  type="button"
                  disabled={busy}
                  onClick={() =>
                    void act(async () => {
                      const more = await request<CollectionPage>(
                        itemsPath(id!, collection),
                      );
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
                  {results.map((book) => (
                    <div className="collection-search-row" key={book.id}>
                      <span>{title(book)}</span>
                      <button
                        type="button"
                        disabled={
                          busy ||
                          collection.items.some(
                            (item) => item.book_id === book.id,
                          )
                        }
                        onClick={() =>
                          void act(async () => {
                            await post(`/api/collections/${id}/items`, {
                              book_id: book.id,
                              expected_revision: collection.revision,
                            });
                            await reload();
                          })
                        }
                      >
                        {t("加入", "Add")}
                      </button>
                    </div>
                  ))}
                </section>
              )}
            </>
          )
        )}
        {creating && (
          <Modal
            title={t("新建合集", "New collection")}
            closeLabel={t("关闭", "Close")}
            close={() => setCreating(false)}
            busy={busy}
          >
            <form
              onSubmit={(event) => {
                event.preventDefault();
                void act(async () => {
                  const params = new URLSearchParams(
                    searchParameters(browse, ""),
                  );
                  const options: Record<string, unknown> = {};
                  for (const [key, value] of params) {
                    if (
                      ["limit", "query", "after_id", "order", "seed"].includes(
                        key,
                      )
                    )
                      continue;
                    options[key] = ["min_rating", "source_rating_min"].includes(
                      key,
                    )
                      ? Number(value)
                      : value;
                  }
                  const value = await post<Collection>("/api/collections", {
                    name,
                    ...(kind === "dynamic"
                      ? { rule: { query: browse.query, options } }
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
              <label>
                {t("名称", "Name")}
                <input
                  value={name}
                  required
                  maxLength={256}
                  onChange={(event) => setName(event.target.value)}
                />
              </label>
              <Segmented
                label={t("合集类型", "Collection type")}
                value={kind}
                change={setKind}
                options={[
                  { value: "static", label: t("静态", "Static") },
                  { value: "dynamic", label: t("动态", "Dynamic") },
                ]}
              />
              {kind === "dynamic" && (
                <>
                  <SearchBox
                    embedded
                    value={browse.query}
                    change={(query) => setBrowse({ ...browse, query })}
                    search={(query) => setBrowse({ ...browse, query })}
                    t={t}
                  />
                  <LibraryFilters value={browse} change={setBrowse} t={t} />
                </>
              )}
              <button
                type="submit"
                className="primary"
                disabled={busy || !name.trim()}
              >
                {t("创建", "Create")}
              </button>
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
            <div className="actions">
              <button
                type="button"
                className="danger"
                onClick={() =>
                  void act(async () => {
                    await request(
                      `/api/collections/${id}?expected_revision=${collection.revision}`,
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
              <button type="button" onClick={() => setDeleting(false)}>
                {t("取消", "Cancel")}
              </button>
            </div>
          </Modal>
        )}
      </main>
    </ContextActions>
  );
}
