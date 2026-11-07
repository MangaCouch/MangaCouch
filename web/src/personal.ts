import type { Book, Personal } from "./types";

type PersonalEdit = (state: Personal) => Personal;
export type SaveStatus = "saved" | "saving" | "error";

export class PersonalQueue {
  private pending: PersonalEdit[] = [];
  /** A delayed edit (reading progress); a newer delayed edit replaces it. */
  private deferred: {
    edit: PersonalEdit;
    timer: ReturnType<typeof setTimeout>;
  } | null = null;
  private running: Promise<void> | null = null;
  private failed = false;
  private closed = false;
  constructor(
    private server: Book,
    private save: (state: Personal, revision: number) => Promise<Book>,
    private load: () => Promise<Book>,
    private notify: (book: Book, status: SaveStatus) => void,
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
  private emit(status: SaveStatus) {
    if (!this.closed) this.notify(this.current(), status);
  }
  /**
   * Queues an edit. With `delay` (ms) the edit waits and is replaced by a newer delayed
   * edit, so frequent progress updates become one save; `flush()` sends it at once.
   */
  update(edit: PersonalEdit, delay = 0) {
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
    if (JSON.stringify(edit(current)) === JSON.stringify(current)) return;
    this.pending.push(edit);
    this.emit(this.failed ? "error" : "saving");
    if (!this.running && !this.failed) this.run(() => this.drain());
  }
  private run(action: () => Promise<void>) {
    // Started synchronously: a flush() right before close() has already sent its request.
    this.running = action().finally(() => {
      this.running = null;
      if (this.pending.length && !this.failed && !this.closed)
        this.run(() => this.drain());
    });
    return this.running;
  }
  private async drain() {
    while (this.pending.length && !this.closed) {
      const count = this.pending.length;
      const desired = this.pending
        .slice(0, count)
        .reduce((state, edit) => edit(state), this.server.personal);
      try {
        this.server = await this.save(desired, this.server.personal_revision);
        this.pending.splice(0, count);
        this.emit(this.pending.length ? "saving" : "saved");
      } catch {
        this.failed = true;
        this.emit("error");
        break;
      }
    }
  }
  async retry() {
    if (this.running || this.closed) return;
    await this.run(async () => {
      try {
        this.server = await this.load();
        this.failed = false;
        this.emit(this.pending.length ? "saving" : "saved");
        await this.drain();
      } catch {
        this.failed = true;
        this.emit("error");
      }
    });
  }
  async reload() {
    if (this.running || this.closed) return;
    await this.run(async () => {
      try {
        const count = this.pending.length;
        this.server = await this.load();
        this.pending.splice(0, count);
        this.failed = false;
        this.emit(this.pending.length ? "saving" : "saved");
        await this.drain();
      } catch {
        this.failed = true;
        this.emit("error");
      }
    });
  }
  async flush() {
    this.commitDeferred();
    while (this.running) await this.running;
    return !this.pending.length;
  }
  close() {
    this.closed = true;
    if (this.deferred) clearTimeout(this.deferred.timer);
    this.deferred = null;
    this.pending = [];
  }
}
