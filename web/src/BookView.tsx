import { lazy, Suspense, useEffect, useRef, useState } from "react";
import { aborted, errorMessage, put, request } from "./api";
import { Detail } from "./Detail";
import { Pages } from "./pages";
import { PersonalQueue } from "./personal";
import type { SaveStatus } from "./personal";
const Reader = lazy(() =>
  import("./Reader").then((module) => ({ default: module.Reader })),
);
import type {
  Book,
  Translate,
  CollectionContext,
  CollectionNavigation,
} from "./types";

type Guard = () => Promise<boolean>;

/** One book: its detail page or the reader, sharing one page cache and save queue. */
export function BookView({
  initialPage,
  id,
  reading,
  t,
  guard,
  collectionId,
  navigate,
  search,
}: {
  collectionId?: string;
  initialPage?: number;
  id: string;
  reading: boolean;
  t: Translate;
  guard: { current: Guard | null };
  navigate: (route: string) => void;
  /** Runs a library search (used by tag links). */
  search: (query: string) => void;
}) {
  const [book, setBook] = useState<Book | null>(null);
  const [status, setStatus] = useState<SaveStatus>("saved");
  const [error, setError] = useState<unknown>(null);
  const [attempt, setAttempt] = useState(0);
  const queue = useRef<PersonalQueue | null>(null);
  const pages = useRef<Pages | null>(null);
  const dirty = useRef(false);
  const [collection, setCollection] = useState<CollectionContext | null>(null);
  const [collectionError, setCollectionError] = useState("");
  const [moving, setMoving] = useState(false);
  const collectionController = useRef<AbortController | null>(null);
  useEffect(() => {
    setCollection(null);
    setCollectionError("");
    if (!collectionId) return;
    const controller = new AbortController();
    collectionController.current = controller;
    void request<CollectionContext>(
      `/api/collections/${collectionId}/context?book_id=${id}`,
      { signal: controller.signal },
    )
      .then((value) => {
        if (!controller.signal.aborted) setCollection(value);
      })
      .catch((error) => {
        if (!aborted(error))
          setCollectionError(
            t(
              "合集信息已变化，请返回合集刷新。",
              "Collection changed. Return to it and refresh.",
            ),
          );
      });
    return () => controller.abort();
  }, [id, collectionId, t]);
  async function moveCollection(direction: -1 | 1) {
    if (!collectionId || moving) return;
    setMoving(true);
    setCollectionError("");
    const signal = collectionController.current?.signal;
    try {
      const value = await request<CollectionContext>(
        `/api/collections/${collectionId}/context?book_id=${id}`,
        { signal },
      );
      if (signal?.aborted) return;
      setCollection(value);
      const next = direction === 1 ? value.next_id : value.previous_id;
      if (next) navigate(`read/${next}/in/${collectionId}`);
    } catch (error) {
      if (!aborted(error))
        setCollectionError(
          t(
            "合集信息已变化或连接失败，请返回合集刷新。",
            "Collection changed or the connection failed. Return and refresh.",
          ),
        );
    } finally {
      setMoving(false);
    }
  }
  useEffect(() => {
    const controller = new AbortController();
    let cache: Pages | null = null;
    setError(null);
    const load = () =>
      request<Book>(`/api/books/${id}`, { signal: controller.signal });
    void load()
      .then((initial) => {
        if (controller.signal.aborted) return;
        cache = new Pages(
          id,
          undefined,
          undefined,
          undefined,
          initial.content.sha256,
        );
        pages.current = cache;
        const changes = new PersonalQueue(
          initial,
          (state, revision) =>
            put<Book>(`/api/books/${id}/personal`, {
              state,
              expected_revision: revision,
            }),
          load,
          (value, saving) => {
            setStatus(saving);
            setBook((old) =>
              old
                ? {
                    ...old,
                    personal: value.personal,
                    personal_revision: value.personal_revision,
                  }
                : value,
            );
          },
        );
        queue.current = changes;
        setBook(initial);
        guard.current = async () => !dirty.current && (await changes.flush());
      })
      .catch((error) => {
        if (!aborted(error)) setError(error);
      });
    return () => {
      controller.abort();
      cache?.dispose();
      pages.current = null;
      // React runs this cleanup before the Reader's own: send the delayed progress
      // edit first, because close() drops it.
      const changes = queue.current;
      if (changes) {
        void changes.flush();
        changes.close();
      }
      queue.current = null;
      guard.current = null;
    };
  }, [id, attempt, guard]);
  useEffect(() => {
    const warn = (event: BeforeUnloadEvent) => {
      if (status !== "saved" || dirty.current) {
        event.preventDefault();
        event.returnValue = "";
      }
    };
    window.addEventListener("beforeunload", warn);
    return () => window.removeEventListener("beforeunload", warn);
  }, [status]);
  if (error)
    return (
      <main className="content">
        <div className="notice error" role="alert">
          {errorMessage(error, t)}
          <button
            type="button"
            onClick={() => setAttempt((value) => value + 1)}
          >
            {t("重试", "Retry")}
          </button>
        </div>
      </main>
    );
  if (!book || !queue.current || !pages.current)
    return (
      <main className="content" role="status">
        {t("加载书籍…", "Loading book…")}
      </main>
    );
  const collectionNav: CollectionNavigation | undefined = collectionId
    ? {
        name: collection?.name || t("合集", "Collection"),
        index: collection?.index,
        count: collection?.count,
        previous: !!collection?.previous_id,
        next: !!collection?.next_id,
        busy: moving,
        error: collectionError,
        back: () => navigate(`collection/${collectionId}`),
        move: (direction) => {
          void moveCollection(direction);
        },
      }
    : undefined;
  const suffix = collectionId ? `/in/${collectionId}` : "";
  const props = { book, queue: queue.current, pages: pages.current, status, t };
  return reading ? (
    <Suspense
      fallback={
        <main role="status">{t("正在打开阅读器…", "Opening reader…")}</main>
      }
    >
      <Reader
        {...props}
        initialPage={initialPage}
        collection={collectionNav}
        exit={() => navigate(`book/${id}${suffix}`)}
      />
    </Suspense>
  ) : (
    <Detail
      {...props}
      navigate={navigate}
      search={search}
      suffix={suffix}
      dirty={dirty}
      update={(value) =>
        setBook({
          ...value,
          personal: queue.current!.current().personal,
          personal_revision: queue.current!.current().personal_revision,
        })
      }
      read={() => navigate(`read/${id}${suffix}`)}
    />
  );
}
