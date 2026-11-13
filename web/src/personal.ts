import type { Book, Personal } from "./types";

type PersonalEdit = ((state: Personal) => Personal) & {
  recordActivity?: boolean;
};
export type SaveStatus = "saved" | "saving" | "error";
/** Sends one state; `keepalive` asks the browser to finish it after the page is gone. */
type Save = (
  state: Personal,
  revision: number,
  keepalive?: boolean,
  recordActivity?: boolean,
  baseState?: Personal,
) => Promise<Book>;

/**
 * Waits before each automatic retry of a save that failed for a transient reason (no
 * response, 5xx). Its length is the retry budget, which a revision conflict (409)
 * that rebases safely also draws from; once spent, the queue stops until the reader
 * picks `retry()` or `reload()`.
 */
export const SAVE_RETRY_DELAYS_MS = [1000, 4000, 15000];

/** How a failed save may continue: replay after a reload, retry later, or stop. */
function failureKind(error: unknown): "conflict" | "transient" | "fatal" {
  const status = (error as { status?: unknown } | null)?.status;
  if (typeof status !== "number") return "transient";
  if (status === 409) return "conflict";
  return status >= 500 || status === 408 || status === 429
    ? "transient"
    : "fatal";
}
const same = (a: unknown, b: unknown) =>
  JSON.stringify(a) === JSON.stringify(b);
/**
 * True when replaying the pending edits on `fresh` would replace a value another
 * device saved since `base`: a field other than the resume page that changed on the
 * server and that the replay would change again. Reading position is last-writer-wins;
 * ratings and the finished flag are never overwritten without asking.
 */
function overwritesOthers(base: Personal, fresh: Personal, desired: Personal) {
  const keys = new Set([
    ...Object.keys(base),
    ...Object.keys(fresh),
    ...Object.keys(desired),
  ]) as Set<keyof Personal>;
  keys.delete("resume_page");
  return [...keys].some(
    (key) => !same(base[key], fresh[key]) && !same(desired[key], fresh[key]),
  );
}

export class PersonalQueue {
  private pending: PersonalEdit[] = [];
  /** A delayed edit (reading progress); a newer delayed edit replaces it. */
  private deferred: {
    edit: PersonalEdit;
    timer: ReturnType<typeof setTimeout>;
  } | null = null;
  private running: Promise<void> | null = null;
  private urgent: Promise<void> | null = null;
  private failed = false;
  /** Why the queue stopped: a conflict needs the reader's choice, never an automatic retry. */
  private conflict = false;
  private closed = false;
  /** The next automatic retry, while one is scheduled. */
  private retryTimer: ReturnType<typeof setTimeout> | null = null;
  /** Automatic retries used since the last successful save. */
  private retries = 0;
  /** Active flush(true) calls keep subsequent queued saves alive through page unloading. */
  private keepaliveFlushes = 0;
  constructor(
    private server: Book,
    private save: Save,
    private load: () => Promise<Book>,
    private notify: (book: Book, status: SaveStatus) => void,
    private missing: () => void = () => {},
  ) {}
  current(): Book {
    const edits = this.deferred
      ? [...this.pending, this.deferred.edit]
      : this.pending;
    return {
      ...this.server,
      personal: edits.reduce(
        (state, edit) => edit(state),
        this.server.personal,
      ),
    };
  }
  /** True when saving stopped because another device changed the same values. */
  conflicted() {
    return this.failed && this.conflict;
  }
  private emit(status: SaveStatus) {
    if (!this.closed) this.notify(this.current(), status);
  }
  /**
   * Queues an edit. With `delay` (ms) the edit waits and is replaced by a newer delayed
   * edit, so frequent progress updates become one save; `flush()` sends it at once.
   */
  update(change: PersonalEdit, delay = 0, recordActivity = false) {
    const edit: PersonalEdit = (state) => change(state);
    edit.recordActivity = recordActivity;
    if (this.closed) return;
    if (delay > 0) {
      if (this.deferred) clearTimeout(this.deferred.timer);
      this.deferred = {
        edit,
        timer: setTimeout(() => this.commitDeferred(), delay),
      };
      return;
    }
    this.commitDeferred();
    this.apply(edit);
  }
  private commitDeferred() {
    const deferred = this.deferred;
    if (!deferred) return;
    clearTimeout(deferred.timer);
    this.deferred = null;
    this.apply(deferred.edit);
  }
  private apply(edit: PersonalEdit) {
    if (this.closed) return;
    const current = this.current().personal;
    if (same(edit(current), current)) return;
    this.pending.push(edit);
    this.emit(this.failed ? "error" : "saving");
    // While a retry waits, the new edit goes out with it.
    if (!this.running && !this.urgent && !this.failed && !this.retryTimer)
      this.run(() => this.drain());
  }
  private run(action: () => Promise<void>) {
    // Started synchronously: a flush() right before close() has already sent its request.
    this.running = action().finally(() => {
      this.running = null;
      if (
        this.pending.length &&
        !this.failed &&
        !this.urgent &&
        !this.closed &&
        !this.retryTimer
      )
        this.run(() => this.drain());
    });
    return this.running;
  }
  private stop(conflict: boolean) {
    this.failed = true;
    this.conflict = conflict;
    this.emit("error");
  }
  private schedule() {
    const delay = SAVE_RETRY_DELAYS_MS[this.retries++]!;
    this.retryTimer = setTimeout(() => this.resume(), delay);
    this.emit("saving");
  }
  private cancelRetry() {
    if (this.retryTimer) clearTimeout(this.retryTimer);
    this.retryTimer = null;
  }
  /** Runs a scheduled retry now (it still counts against the retry budget). */
  private resume() {
    this.cancelRetry();
    if (!this.running && !this.failed && !this.closed && this.pending.length)
      this.run(() => this.drain());
  }
  private disappeared(error: unknown) {
    if ((error as { status?: number } | null)?.status !== 404) return false;
    this.pending = [];
    if (this.deferred) clearTimeout(this.deferred.timer);
    this.deferred = null;
    this.cancelRetry();
    this.failed = false;
    this.emit("saved");
    this.missing();
    return true;
  }
  canDiscardProgress() {
    if (!this.failed || this.running || this.retryTimer) return false;
    let value = this.server.personal;
    for (const edit of this.pending) {
      const next = edit(value);
      const { resume_page: _before, ...before } = value;
      const { resume_page: _after, ...after } = next;
      if (!same(before, after)) return false;
      value = next;
    }
    return true;
  }
  private accept(batch: PersonalEdit[], saved: Book) {
    if (saved.personal_revision >= this.server.personal_revision)
      this.server = saved;
    this.pending = this.pending.filter((edit) => !batch.includes(edit));
  }
  /** A tab can disappear before an older response arrives. Send its latest state now. */
  private saveBeforeUnload() {
    if (this.urgent || !this.pending.length) return;
    const batch = this.pending.slice();
    const base = this.server;
    const desired = batch.reduce((state, edit) => edit(state), base.personal);
    this.urgent = this.save(
      desired,
      base.personal_revision,
      true,
      batch.some((edit) => edit.recordActivity),
      base.personal,
    )
      .then((saved) => {
        this.accept(batch, saved);
        this.cancelRetry();
        this.failed = this.conflict = false;
        this.retries = 0;
        this.emit(this.pending.length ? "saving" : "saved");
      })
      .catch((error) => {
        if (this.closed || batch.every((edit) => !this.pending.includes(edit)))
          return;
        if (this.disappeared(error)) return;
        // The edits stay pending. A transient failure (the tab was only hidden, say)
        // retries on the usual schedule, as a failed ordinary save does.
        const kind = failureKind(error);
        if (kind === "transient" && this.retries < SAVE_RETRY_DELAYS_MS.length) {
          if (!this.retryTimer) this.schedule();
          return;
        }
        this.stop(kind === "conflict");
      })
      .finally(() => {
        this.urgent = null;
        if (
          !this.running &&
          !this.closed &&
          !this.failed &&
          !this.retryTimer &&
          this.pending.length
        )
          this.run(() => this.drain());
      });
  }
  private async drain() {
    while (
      this.pending.length &&
      !this.closed &&
      !this.urgent &&
      !this.failed
    ) {
      const batch = this.pending.slice();
      const replay = (state: Personal) =>
        batch.reduce((value, edit) => edit(value), state);
      const keepalive = this.keepaliveFlushes > 0;
      try {
        const saved = await this.save(
          replay(this.server.personal),
          this.server.personal_revision,
          keepalive,
          batch.some((edit) => edit.recordActivity),
        );
        this.accept(batch, saved);
        this.retries = 0;
        // A retry that a failed page-unload snapshot scheduled is this loop's job now.
        this.cancelRetry();
        if (!this.pending.length) this.failed = this.conflict = false;
        this.emit(
          this.failed ? "error" : this.pending.length ? "saving" : "saved",
        );
        continue;
      } catch (error) {
        if (
          this.closed ||
          !!this.urgent ||
          batch.every((edit) => !this.pending.includes(edit)) ||
          this.disappeared(error)
        )
          return;
        const kind = failureKind(error);
        const budget = this.retries < SAVE_RETRY_DELAYS_MS.length;
        if (kind === "transient" && budget) return this.schedule();
        if (kind !== "conflict" || !budget)
          return this.stop(kind === "conflict");
      }
      // A revision conflict: another device (or a write whose response was lost) saved
      // first. Reload, and replay on the fresh state only when that keeps their values.
      this.retries++;
      let fresh: Book;
      try {
        fresh = await this.load();
      } catch (error) {
        if (this.closed || this.disappeared(error)) return;
        const budget = this.retries < SAVE_RETRY_DELAYS_MS.length;
        if (failureKind(error) === "transient" && budget)
          return this.schedule();
        return this.stop(false);
      }
      if (this.closed) return;
      this.commitDeferred();
      const base = this.server.personal;
      const desired = this.pending.reduce(
        (state, edit) => edit(state),
        fresh.personal,
      );
      if (overwritesOthers(base, fresh.personal, desired))
        return this.stop(true);
      this.server = fresh;
    }
  }
  /** Stops waiting and replays every pending edit on the latest server state. */
  async retry() {
    if (this.running || this.closed) return;
    this.cancelRetry();
    this.retries = 0;
    await this.run(async () => {
      try {
        this.server = await this.load();
        this.failed = false;
        this.conflict = false;
        this.emit(this.pending.length ? "saving" : "saved");
        await this.drain();
      } catch (error) {
        if (!this.disappeared(error)) this.stop(false);
      }
    });
  }
  /** Discards the edits pending so far and shows the latest server state. */
  async reload() {
    if (this.running || this.closed) return;
    this.cancelRetry();
    this.retries = 0;
    this.pending = [];
    if (this.deferred) clearTimeout(this.deferred.timer);
    this.deferred = null;
    await this.run(async () => {
      try {
        this.server = await this.load();
        this.failed = false;
        this.conflict = false;
        this.emit(this.pending.length ? "saving" : "saved");
        await this.drain();
      } catch (error) {
        if (!this.disappeared(error)) this.stop(false);
      }
    });
  }
  /**
   * The connection is back: a waiting retry runs now, and a queue stopped by
   * connection failures starts a new bounded round. A conflict still waits for the reader.
   */
  online() {
    if (this.closed || this.running) return;
    if (this.retryTimer) return this.resume();
    if (!this.failed || this.conflict || !this.pending.length) return;
    this.failed = false;
    this.retries = 0;
    this.emit("saving");
    this.run(() => this.drain());
  }
  /**
   * Sends pending edits now, a waiting retry included, and resolves once nothing is
   * in flight: true when everything is saved. With `keepalive` the requests it starts
   * outlive the page (for pagehide).
   */
  async flush(keepalive = false) {
    if (keepalive) this.keepaliveFlushes++;
    try {
      const alreadyRunning = !!this.running;
      this.commitDeferred();
      if (keepalive && alreadyRunning) this.saveBeforeUnload();
      if (this.retryTimer) this.resume();
      while (this.running || this.urgent)
        await Promise.all([this.running, this.urgent]);
      return !this.pending.length || this.canDiscardProgress();
    } finally {
      if (keepalive) this.keepaliveFlushes--;
    }
  }
  close() {
    this.closed = true;
    if (this.deferred) clearTimeout(this.deferred.timer);
    this.deferred = null;
    this.cancelRetry();
    this.pending = [];
  }
}
