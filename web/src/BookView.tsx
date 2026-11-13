import { toast } from "./feedback";
import { NavigationContext } from "./navigation";
import { RouteLink } from "./navigation";
import type { GuardRef } from "./navigation";
import { useContext, lazy, Suspense, useEffect, useRef, useState } from "react";
import { aborted, ApiError, errorMessage, request } from "./api";
import { Detail, unavailable } from "./Detail";
import { ErrorBoundary } from "./ErrorBoundary";
import { Pages } from "./pages";
import { PersonalQueue } from "./personal";
import { useBookTitle } from "./PreferenceContext";
import { FileWarning } from "lucide-react";
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

/** One book: its detail page or the reader, sharing one page cache and save queue. */
export function BookView({
  initialPage,
  consumePage,
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
  consumePage?: () => void;
  id: string;
  reading: boolean;
  t: Translate;
  guard: GuardRef;
  navigate: (route: string) => void;
  /** Runs a library search (used by tag links). */
  search: (query: string) => void;
}) {
  const title = useBookTitle();
  const backNavigation = useContext(NavigationContext);
  const translate = useRef(t);
  translate.current = t;
  const navigation = useRef(navigate);
  navigation.current = navigate;
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
        if (!aborted(error)) setCollectionError(errorMessage(error, t));
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
      // The next book takes the reader's place: its details are where both its back
      // button and the browser's back lead.
      const route = `read/${next}/in/${collectionId}`;
      if (next && backNavigation) backNavigation.replace(`#${route}`);
      else if (next) navigate(route);
    } catch (error) {
      if (!aborted(error)) setCollectionError(errorMessage(error, t));
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
          initial.page_version,
        );
        pages.current = cache;
        const changes = new PersonalQueue(
          initial,
          (state, revision, keepalive, recordActivity, baseState) =>
            request<Book>(
              `/api/books/${id}/personal?expected_revision=${revision}`,
              {
                method: "PUT",
                body: JSON.stringify({
                  state,
                  record_activity: recordActivity ?? false,
                  ...(baseState ? { base_state: baseState } : {}),
                }),
                keepalive,
              },
            ),
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
          () => {
            guard.current = null;
            toast(
              translate.current(
                "这本书已被移除。",
                "This book has been removed.",
              ),
            );
            navigation.current("");
          },
        );
        queue.current = changes;
        setBook(initial);
        guard.current = async (next) => {
          if (dirty.current) return false;
          if (next && new RegExp(`^#(?:book|read)/${id}(?:/|$)`).test(next))
            return true;
          return changes.flush();
        };
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
    // Saves waiting on a connection failure go out as soon as the browser is back online.
    const online = () => queue.current?.online();
    window.addEventListener("online", online);
    return () => window.removeEventListener("online", online);
  }, []);
  useEffect(() => {
    const warn = (event: BeforeUnloadEvent) => {
      if (
        (status !== "saved" && !queue.current?.canDiscardProgress()) ||
        dirty.current
      ) {
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
          {!(error instanceof ApiError && error.status === 404) && (
            <button
              type="button"
              onClick={() => setAttempt((value) => value + 1)}
            >
              {t("重试", "Retry")}
            </button>
          )}
          <RouteLink
            route={collectionId ? `collection/${collectionId}` : ""}
            navigate={navigate}
          >
            {collectionId
              ? t("返回合集", "Back to collection")
              : t("返回书库", "Back to library")}
          </RouteLink>
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
        back: () =>
          backNavigation
            ? backNavigation.navigate(`#collection/${collectionId}`)
            : navigate(`collection/${collectionId}`),
        move: (direction) => {
          void moveCollection(direction);
        },
      }
    : undefined;
  const suffix = collectionId ? `/in/${collectionId}` : "";
  const props = { book, queue: queue.current, pages: pages.current, status, t };
  const exit = () =>
    backNavigation
      ? backNavigation.navigate(backNavigation.target)
      : navigate(`book/${id}${suffix}`);
  if (reading && book.availability && book.availability !== "available") {
    const reason = unavailable(book.availability, t);
    return (
      <main className="content">
        <h1>{title(book)}</h1>
        <div className="notice error availability" role="alert">
          <FileWarning size={18} aria-hidden="true" />
          <strong>{reason.label}</strong> {reason.detail}
        </div>
        <button onClick={exit}>{t("返回书籍详情", "Back to the book")}</button>{" "}
        <RouteLink route={"settings/files"} navigate={navigate}>
          {t("检查文件", "Check file")}
        </RouteLink>
      </main>
    );
  }
  return reading ? (
    <ErrorBoundary
      t={t}
      resetKey={id}
      back={{ label: t("返回书籍详情", "Back to the book"), action: exit }}
    >
      <Suspense
        fallback={
          <main role="status">{t("正在打开阅读器…", "Opening reader…")}</main>
        }
      >
        <Reader
          {...props}
          initialPage={initialPage}
          consumePage={consumePage}
          collection={collectionNav}
          exit={exit}
        />
      </Suspense>
    </ErrorBoundary>
  ) : (
    <Detail
      {...props}
      navigate={navigate}
      search={search}
      suffix={suffix}
      dirty={dirty}
      update={(value) =>
        setBook((old) => ({
          ...value,
          // Mutation responses are the short projection; keep the full one's extras.
          tag_counts: value.tag_counts ?? old?.tag_counts,
          page_sizes: value.page_sizes ?? old?.page_sizes,
          personal: queue.current!.current().personal,
          personal_revision: queue.current!.current().personal_revision,
        }))
      }
      read={() => navigate(`read/${id}${suffix}`)}
    />
  );
}
