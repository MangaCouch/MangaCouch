import {
  PageLimitError,
  aborted,
  failure,
  imageUrl,
  timeoutSignal,
} from "./api";

/** The server's IMAGE_VERSION: bumped whenever covers and thumbnails render differently. */
const RENDER_VERSION = 2;
/** Content hash, render version and the server's per-book refresh revision. */
const imageVersion = (sha256: string | undefined, revision: number) =>
  sha256 ? `${sha256.slice(0, 16)}.${RENDER_VERSION}.${revision}` : "";
/** Versioned page thumbnail URL; immutable-cached by the server while `v` matches the content. */
export const thumbnailPath = (
  bookId: string,
  page: number,
  sha256: string | undefined,
  revision: number,
) =>
  imageUrl(
    `/api/books/${encodeURIComponent(bookId)}/thumbnails/${page}`,
    imageVersion(sha256, revision),
  );
/** Versioned cover URL; immutable-cached by the server while `v` matches the content. */
export const coverUrl = (
  bookId: string,
  sha256: string | undefined,
  revision: number,
) =>
  imageUrl(
    `/api/books/${encodeURIComponent(bookId)}/cover`,
    imageVersion(sha256, revision),
  );

type State = "done" | "failed";
type Waiter = {
  resolve: (blob: Blob) => void;
  reject: (error: unknown) => void;
  cleanup: () => void;
};
type Task = {
  page: number;
  background: boolean;
  /** Keep the page in memory; otherwise it only warms the browser's HTTP cache. */
  store: boolean;
  started: boolean;
  backgroundSlot: boolean;
  controller: AbortController;
  waiters: Set<Waiter>;
  expiry?: ReturnType<typeof setTimeout>;
  maxBytes: number;
};

/** A page response without image bytes; never cached as a successful page. */
export class EmptyPageError extends Error {
  constructor() {
    super("The server returned an empty page");
  }
}

/**
 * Pages kept in memory (compressed bytes) on each side of the reading position. Every
 * downloaded page also stays in the browser's HTTP cache (the page URLs are immutable),
 * and reading one back from there is nearly as quick: measured in Chrome with 0.8 MB,
 * 1600x2400 JPEG pages, about 1-1.5 ms, against about 10 ms to decode a page either
 * way. So memory only holds the pages around the reader, never whole books, and a tab
 * is never at risk of being killed for memory (iOS Safari reloads tabs past ~1.5 GB).
 */
export const WINDOW = 5;
/** A safety cap on the window's bytes, for books of huge scans. */
const BUDGET = 256 * 1024 ** 2;

export class Pages {
  private cache = new Map<number, Blob>();
  private bytes = 0;
  /** The reading position; eviction starts with the pages farthest from it. */
  private position = 0;
  // One object URL per cached page, so a pre-decoded image is reused when it is shown.
  private urls = new Map<number, string>();
  private tasks = new Map<number, Task>();
  private states = new Map<number, State>();
  private listeners = new Set<() => void>();
  private counts = { done: 0, failed: 0 };
  private active = 0;
  foregroundSuccess = 0;
  private closed = false;
  constructor(
    readonly bookId: string,
    private budget = BUDGET,
    /** Most pages kept in memory; by default only the window and budget limit it. */
    private capacity = Infinity,
    private responseLimit = 128 * 1024 ** 2,
    readonly sha256?: string,
    readonly pageVersion?: string,
  ) {}
  /** Moves the reading position; pages that leave the window leave memory. */
  focus(page: number) {
    this.position = page;
    this.trim();
  }
  /** Whether a page belongs in memory: within WINDOW pages of the reading position. */
  near(page: number) {
    return Math.abs(page - this.position) <= WINDOW;
  }
  /**
   * Evicts the pages outside the window, then the pages farthest from the reading
   * position (behind it first on a tie) until the cache fits its budget; `keep` (a page
   * the reader asked for, before its position moves there) is never evicted.
   */
  private trim(keep?: number) {
    for (const page of [...this.cache.keys()])
      if (page !== keep && !this.near(page)) this.drop(page);
    while (this.cache.size > this.capacity || this.bytes > this.budget) {
      let victim: number | undefined;
      let worst = -1;
      for (const page of this.cache.keys()) {
        if (page === keep) continue;
        const distance = Math.abs(page - this.position);
        if (distance > worst || (distance === worst && page < this.position)) {
          victim = page;
          worst = distance;
        }
      }
      if (victim === undefined) return;
      this.drop(victim);
    }
  }
  /** Removes a page from the cache and revokes its shared object URL. */
  private drop(page: number) {
    const blob = this.cache.get(page);
    if (blob) this.bytes -= blob.size;
    this.cache.delete(page);
    const url = this.urls.get(page);
    if (url) URL.revokeObjectURL(url);
    this.urls.delete(page);
  }
  /** A stable object URL for a cached page (undefined when it is not cached). */
  url(page: number) {
    const blob = this.cache.get(page);
    if (!blob) return undefined;
    let url = this.urls.get(page);
    if (!url) {
      url = URL.createObjectURL(blob);
      this.urls.set(page, url);
    }
    return url;
  }
  /**
   * Whether the page was downloaded before, so the browser's HTTP cache serves it in
   * about a millisecond; the reader then shows it directly instead of thumbnail first.
   */
  downloaded(page: number) {
    return this.states.get(page) === "done";
  }
  has(page: number) {
    return this.cache.has(page);
  }
  /** Pages from `page` on that are in memory or already fetched into the HTTP cache. */
  bufferedAhead(page: number, total: number) {
    let end = page;
    while (end < total && (this.has(end) || this.states.get(end) === "done"))
      end++;
    return end - page;
  }
  state(page: number) {
    return this.states.get(page);
  }
  subscribe(listener: () => void) {
    this.listeners.add(listener);
    return () => {
      this.listeners.delete(listener);
    };
  }
  progress(total: number) {
    const { done, failed } = this.counts;
    return { done, failed, total, waiting: total - done - failed };
  }
  private mark(page: number, state?: State) {
    const before = this.states.get(page);
    if (before === state) return;
    if (before) this.counts[before]--;
    this.states.delete(page);
    if (state) {
      this.states.set(page, state);
      this.counts[state]++;
    }
    this.listeners.forEach((listener) => listener());
  }
  retryFailed(total: number) {
    for (const [page, state] of this.states) {
      if (page < total && state === "failed") this.mark(page);
    }
  }
  invalidate(page: number) {
    this.drop(page);
    this.mark(page, "failed");
  }
  /**
   * The page's bytes. `store: false` fetches without keeping the page in memory: the
   * versioned page URL is immutable, so the browser's HTTP cache keeps it for later.
   */
  async get(
    page: number,
    signal: AbortSignal,
    background = false,
    store = true,
    maxBytes = this.responseLimit,
  ): Promise<Blob> {
    signal.throwIfAborted();
    if (this.closed) throw new DOMException("Reader closed", "AbortError");
    const hit = this.cache.get(page);
    if (hit) return hit;
    let task = this.tasks.get(page);
    if (!task || task.controller.signal.aborted) {
      task = {
        page,
        background,
        store,
        started: false,
        backgroundSlot: false,
        controller: new AbortController(),
        waiters: new Set(),
        maxBytes,
      };
      this.tasks.set(page, task);
    }
    clearTimeout(task.expiry);
    if (!background) {
      task.background = false;
      task.maxBytes = this.responseLimit;
    }
    if (store) task.store = true;
    const pending = task;
    return new Promise<Blob>((resolve, reject) => {
      const cancel = () => {
        pending.waiters.delete(waiter);
        waiter.cleanup();
        reject(signal.reason);
        if (!pending.waiters.size) {
          if (!pending.started) {
            pending.controller.abort();
            if (this.tasks.get(page) === pending) this.tasks.delete(page);
          } else {
            pending.expiry = setTimeout(() => {
              if (!pending.waiters.size) pending.controller.abort();
            }, 500);
          }
        }
        this.pump();
      };
      const waiter = {
        resolve,
        reject,
        cleanup: () => signal.removeEventListener("abort", cancel),
      };
      pending.waiters.add(waiter);
      signal.addEventListener("abort", cancel, { once: true });
      this.pump();
    });
  }
  private pump() {
    if (this.closed) return;
    while (this.active < 2) {
      const tasks = [...this.tasks.values()];
      const next =
        tasks.find((task) => !task.started && !task.background) ??
        (!tasks.some((task) => task.started && task.backgroundSlot)
          ? tasks.find((task) => !task.started)
          : undefined);
      if (!next) return;
      next.started = true;
      next.backgroundSlot = next.background;
      this.active++;
      void this.load(next);
    }
  }
  private async download(task: Task): Promise<Blob> {
    const timed = timeoutSignal(task.controller.signal, 60_000);
    try {
      return await this.transfer(task, timed.signal);
    } finally {
      timed.release();
    }
  }
  private async transfer(task: Task, signal: AbortSignal): Promise<Blob> {
    const response = await fetch(
      imageUrl(
        `/api/books/${encodeURIComponent(this.bookId)}/pages/${task.page}`,
        this.pageVersion || this.sha256?.slice(0, 16),
      ),
      {
        credentials: "same-origin",
        signal,
        priority: task.background ? "low" : "high",
      },
    );
    signal.throwIfAborted();
    if (!response.ok) {
      if (response.status === 401) window.dispatchEvent(new Event("mc-locked"));
      throw await failure(response);
    }
    const type =
      response.headers.get("Content-Type")?.split(";", 1)[0]?.trim() ||
      "image/png";
    if (
      Number(response.headers.get("Content-Length")) >
      Math.min(this.responseLimit, task.maxBytes)
    ) {
      await response.body?.cancel();
      throw new PageLimitError(Math.min(this.responseLimit, task.maxBytes));
    }
    const reader = response.body?.getReader();
    if (!reader) throw new EmptyPageError();
    const chunks: BlobPart[] = [];
    let size = 0;
    const cancel = () => {
      void reader.cancel().catch(() => {});
    };
    signal.addEventListener("abort", cancel, { once: true });
    try {
      while (true) {
        const { value, done } = await reader.read();
        signal.throwIfAborted();
        if (done) break;
        size += value.byteLength;
        if (size > Math.min(this.responseLimit, task.maxBytes))
          throw new PageLimitError(Math.min(this.responseLimit, task.maxBytes));
        chunks.push(value);
      }
      if (!size) throw new EmptyPageError();
      return new Blob(chunks, { type });
    } catch (error) {
      await reader.cancel().catch(() => {});
      throw error;
    } finally {
      signal.removeEventListener("abort", cancel);
      reader.releaseLock();
    }
  }
  private async load(task: Task) {
    try {
      // A page read back from the HTTP cache stays "done" while it loads; only a
      // failed one goes back to waiting.
      if (this.states.get(task.page) === "failed") this.mark(task.page);
      const blob = await this.download(task);
      task.controller.signal.throwIfAborted();
      if (
        task.store &&
        blob.size <= this.budget &&
        (this.near(task.page) || !task.background)
      ) {
        this.drop(task.page);
        this.cache.set(task.page, blob);
        this.bytes += blob.size;
        // A page the reader asked for stays; a background one may be the first to go.
        this.trim(task.background ? undefined : task.page);
      }
      if (!task.background) this.foregroundSuccess++;
      this.mark(task.page, "done");
      task.waiters.forEach((waiter) => waiter.resolve(blob));
    } catch (error) {
      const budgetOnly =
        task.background &&
        error instanceof PageLimitError &&
        task.maxBytes < this.responseLimit;
      if (!aborted(error) && !task.controller.signal.aborted && !budgetOnly)
        this.mark(task.page, "failed");
      task.waiters.forEach((waiter) => waiter.reject(error));
    } finally {
      clearTimeout(task.expiry);
      task.waiters.forEach((waiter) => waiter.cleanup());
      if (this.tasks.get(task.page) === task) this.tasks.delete(task.page);
      this.active--;
      this.pump();
    }
  }
  dispose() {
    this.closed = true;
    for (const task of this.tasks.values()) {
      clearTimeout(task.expiry);
      task.controller.abort();
      for (const waiter of task.waiters) {
        waiter.cleanup();
        waiter.reject(new DOMException("Reader closed", "AbortError"));
      }
      task.waiters.clear();
    }
    this.tasks.clear();
    for (const page of [...this.urls.keys()]) this.drop(page);
    this.cache.clear();
    this.bytes = 0;
    this.states.clear();
    this.counts = { done: 0, failed: 0 };
    this.listeners.clear();
  }
}
