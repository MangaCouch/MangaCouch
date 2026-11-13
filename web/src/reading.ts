export function adjacent(
  pages: number[],
  current: number,
  direction: 1 | -1,
): number | undefined {
  return direction === 1
    ? pages.find((page) => page > current)
    : [...pages].reverse().find((page) => page < current);
}

/** Clamps a saved resume position into the book's page range. */
export function startingPage(count: number, resume: number | null): number {
  // A page from a hand-edited link may not be a number at all.
  return Math.max(
    0,
    Math.min(Number.isFinite(resume) ? (resume as number) : 0, count - 1),
  );
}

export function readingStatus(personal: { completed: boolean; resume_page: number | null }): "unread" | "reading" | "finished" {
  return personal.completed ? "finished" : personal.resume_page === null ? "unread" : "reading";
}

export type ReadingMode = "single" | "spread" | "scroll";

/** Width/height ratios of every page from the book's recorded page sizes (`Book.page_sizes`). */
export function pageRatios(
  sizes: readonly (readonly [number, number])[] | undefined,
): Map<number, number> {
  const ratios = new Map<number, number>();
  sizes?.forEach(([width, height], page) => {
    if (width > 0 && height > 0) ratios.set(page, width / height);
  });
  return ratios;
}
/** Pages wider than tall; they are always shown on their own in spread mode. */
export const widePages = (ratios: ReadonlyMap<number, number>) =>
  new Set([...ratios].filter(([, ratio]) => ratio > 1).map(([page]) => page));

export function spreads(
  pages: number[],
  wide: ReadonlySet<number>,
  coverSolo: boolean,
): number[][] {
  const result: number[][] = [];
  for (let index = 0; index < pages.length;) {
    const first = pages[index]!;
    const second = pages[index + 1];
    if (
      (coverSolo && first === 0) ||
      wide.has(first) ||
      second === undefined ||
      wide.has(second)
    ) {
      result.push([first]);
      index++;
    } else {
      result.push([first, second]);
      index += 2;
    }
  }
  return result;
}

/** Vertical gap between rows in scroll mode, in CSS pixels. */
export const ROW_GAP = 16;
export interface ScrollRow {
  page: number;
  top: number;
  height: number;
}
export function scrollRows(
  pages: number[],
  width: number,
  ratios: ReadonlyMap<number, number>,
  minimumHeight = 120,
  fixedHeight?: number,
): ScrollRow[] {
  let top = 0;
  return pages.map((page) => {
    const height =
      fixedHeight ??
      Math.max(minimumHeight, width / (ratios.get(page) || 0.72));
    const row = { page, top, height };
    top += height + ROW_GAP;
    return row;
  });
}
function rowAt(rows: ScrollRow[], position: number): number {
  let low = 0,
    high = rows.length - 1;
  while (low < high) {
    const middle = Math.floor((low + high) / 2);
    if (rows[middle]!.top + rows[middle]!.height + ROW_GAP <= position)
      low = middle + 1;
    else high = middle;
  }
  return low;
}
export function scrollWindow(rows: ScrollRow[], top: number, height: number) {
  const first = rowAt(rows, top),
    last = rowAt(rows, top + Math.max(0, height - 1));
  const active = rowAt(rows, top + Math.min(height * 0.1, 40));
  const start = Math.max(0, first - 1),
    end = Math.min(rows.length, last + 2);
  return { first, last, active, start, end };
}
export function scrollHeight(rows: ScrollRow[]): number {
  const last = rows.at(-1);
  return last ? last.top + last.height : 0;
}
export function anchoredTop(
  previous: ScrollRow[],
  next: ScrollRow[],
  top: number,
): number {
  const anchor = previous[rowAt(previous, top)];
  if (!anchor) return 0;
  const replacement = next.find((row) => row.page === anchor.page);
  return replacement
    ? replacement.top +
        Math.min(1, Math.max(0, (top - anchor.top) / anchor.height)) *
          replacement.height
    : top;
}

interface PlaybackContext {
  key: string;
  ready: boolean;
  atEnd: boolean;
  visible: boolean;
  blocked: boolean;
}
interface Clock {
  set(callback: () => void, delay: number): unknown;
  clear(id: unknown): void;
}
const browserClock: Clock = {
  set: (callback, delay) => setTimeout(callback, delay),
  clear: (id) => clearTimeout(id as ReturnType<typeof setTimeout>),
};

export class Autoplay {
  playing = false;
  private delay = 8000;
  private timer: unknown;
  private generation = 0;
  private context: PlaybackContext = {
    key: "",
    ready: false,
    atEnd: false,
    visible: true,
    blocked: false,
  };
  constructor(
    private advance: () => void,
    private notify: (playing: boolean) => void,
    private clock = browserClock,
  ) {}
  private cancel() {
    this.generation++;
    if (this.timer !== undefined) this.clock.clear(this.timer);
    this.timer = undefined;
  }
  private schedule() {
    this.cancel();
    if (!this.playing || !this.context.ready) return;
    const generation = this.generation;
    this.timer = this.clock.set(() => {
      if (
        generation !== this.generation ||
        !this.playing ||
        !this.context.ready ||
        !this.context.visible ||
        this.context.blocked ||
        this.context.atEnd
      )
        return;
      this.cancel();
      this.context = { ...this.context, ready: false };
      this.advance();
    }, this.delay);
  }
  update(context: PlaybackContext) {
    const changed = Object.keys(context).some(
      (key) =>
        context[key as keyof PlaybackContext] !==
        this.context[key as keyof PlaybackContext],
    );
    this.context = context;
    if (!context.visible || context.blocked || context.atEnd) {
      this.pause();
      return;
    }
    if (changed) this.schedule();
  }
  setSeconds(seconds: number) {
    if (!Number.isFinite(seconds) || seconds < 2 || seconds > 60)
      throw new Error("Invalid playback interval");
    this.delay = seconds * 1000;
    this.schedule();
  }
  start() {
    if (this.context.atEnd || !this.context.visible || this.context.blocked)
      return;
    this.playing = true;
    this.notify(true);
    this.schedule();
  }
  pause() {
    this.cancel();
    if (this.playing) {
      this.playing = false;
      this.notify(false);
    }
  }
  dispose() {
    this.cancel();
    this.playing = false;
  }
}
