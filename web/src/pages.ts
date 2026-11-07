import { ApiError, PageLimitError, aborted, pagePath } from "./api";

/** Cache-busting version for cover and thumbnail URLs (contract: first 16 hex chars of the content hash). */
export const imageVersion = (sha256: string | undefined) =>
  (sha256 ?? "").slice(0, 16);
const versioned = (path: string, sha256: string | undefined) =>
  imageVersion(sha256) ? `${path}?v=${imageVersion(sha256)}` : path;
/** Versioned page thumbnail URL; immutable-cached by the server while `v` matches the content. */
export const thumbnailPath = (
  bookId: string,
  page: number,
  sha256: string | undefined,
) =>
  versioned(
    `/api/books/${encodeURIComponent(bookId)}/thumbnails/${page}`,
    sha256,
  );
/** Versioned cover URL; immutable-cached by the server while `v` matches the content. */
export const coverUrl = (bookId: string, sha256: string | undefined) =>
  versioned(`/api/books/${encodeURIComponent(bookId)}/cover`, sha256);

type State = "done" | "failed";
type Waiter = {
  resolve: (blob: Blob) => void;
  reject: (error: unknown) => void;
  cleanup: () => void;
};
type Task = {
  page: number;
  background: boolean;
  started: boolean;
  controller: AbortController;
  waiters: Set<Waiter>;
  expiry?: ReturnType<typeof setTimeout>;
};

/** A page response without image bytes; never cached as a successful page. */
export class EmptyPageError extends Error {
  constructor() {
    super("The server returned an empty page");
  }
}

export class Pages {
  private cache = new Map<number, Blob>();
  private tasks = new Map<number, Task>();
  private states = new Map<number, State>();
  private listeners = new Set<() => void>();
  private counts = { done: 0, failed: 0 };
  private active = 0;
  private closed = false;
  constructor(
    readonly bookId: string,
    private budget = 48 * 1024 ** 2,
    private capacity = 12,
    private responseLimit = 128 * 1024 ** 2,
    readonly sha256?: string,
  ) {}
  focus(page: number) {
    for (const key of this.cache.keys()) {
      if (key < page - 3 || key > page + 8) this.cache.delete(key);
    }
  }
  warmEnough() {
    return (
      this.cache.size >= this.capacity - 2 ||
      [...this.cache.values()].reduce((sum, blob) => sum + blob.size, 0) >=
        this.budget * 0.7
    );
  }
  has(page: number) {
    return this.cache.has(page);
  }
  bufferedAhead(page: number, total: number) {
    let end = page;
    while (end < total && this.has(end)) end++;
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
    this.cache.delete(page);
    this.mark(page, "failed");
  }
  async get(
    page: number,
    signal: AbortSignal,
    background = false,
  ): Promise<Blob> {
    signal.throwIfAborted();
    if (this.closed) throw new DOMException("Reader closed", "AbortError");
    const hit = this.cache.get(page);
    if (hit) {
      this.cache.delete(page);
      this.cache.set(page, hit);
      return hit;
    }
    let task = this.tasks.get(page);
    if (!task || task.controller.signal.aborted) {
      task = {
        page,
        background,
        started: false,
        controller: new AbortController(),
        waiters: new Set(),
      };
      this.tasks.set(page, task);
    }
    clearTimeout(task.expiry);
    if (!background) task.background = false;
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
        (!tasks.some((task) => task.started && task.background)
          ? tasks.find((task) => !task.started)
          : undefined);
      if (!next) return;
      next.started = true;
      this.active++;
      void this.load(next);
    }
  }
  private async download(task: Task): Promise<Blob> {
    const signal = task.controller.signal;
    const response = await fetch(
      pagePath(this.bookId, task.page, this.sha256),
      {
        credentials: "same-origin",
        signal,
        priority: task.background ? "low" : "high",
      },
    );
    signal.throwIfAborted();
    if (!response.ok) {
      await response.body?.cancel();
      if (response.status === 401) window.dispatchEvent(new Event("mc-locked"));
      throw new ApiError(response.status);
    }
    const type =
      response.headers.get("Content-Type")?.split(";", 1)[0]?.trim() ||
      "image/png";
    if (Number(response.headers.get("Content-Length")) > this.responseLimit) {
      await response.body?.cancel();
      throw new PageLimitError(this.responseLimit);
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
        if (size > this.responseLimit)
          throw new PageLimitError(this.responseLimit);
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
      this.mark(task.page);
      const blob = await this.download(task);
      task.controller.signal.throwIfAborted();
      const bytes = () =>
        [...this.cache.values()].reduce((sum, value) => sum + value.size, 0);
      if (blob.size <= this.budget) {
        this.cache.set(task.page, blob);
        while (this.cache.size > this.capacity || bytes() > this.budget) {
          const evicted = this.cache.keys().next().value!;
          this.cache.delete(evicted);
        }
      }
      this.mark(task.page, "done");
      task.waiters.forEach((waiter) => waiter.resolve(blob));
    } catch (error) {
      if (!aborted(error) && !task.controller.signal.aborted)
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
    this.cache.clear();
    this.states.clear();
    this.counts = { done: 0, failed: 0 };
    this.listeners.clear();
  }
}
