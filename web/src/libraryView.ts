import { storageKey } from "./device";
import { initialBrowse } from "./LibraryFilters";
import type { Browse } from "./LibraryFilters";
import type { LibraryScroll } from "./Library";
import { languages, workTypes } from "./Pills";

/** What the library shelf shows: search, filters, page and scroll position. */
export interface LibraryView {
  browse: Browse;
  page: number;
  scroll: LibraryScroll | null;
}

const READING: Browse["reading"][] = [
  "all",
  "unread",
  "in_progress",
  "completed",
];
const stars = (value: unknown) =>
  typeof value === "string" &&
  /^[0-5](\.5)?$/.test(value) &&
  Number(value) >= 0.5 &&
  Number(value) <= 5;
const optional = (value: unknown, allowed: string[]) =>
  typeof value === "string" && allowed.includes(value) ? value : undefined;

/** URL filters; anything unknown falls back to its default. */
function filtersFrom(stored: Record<string, unknown>, base: Browse): Browse {
  const rating = stored.rating;
  const sourceRating = stored.sourceRating;
  return {
    ...base,
    category: optional(stored.category, workTypes),
    language: optional(
      stored.language,
      languages.map((language) => language.toLowerCase()),
    ),
    reading: READING.includes(stored.reading as Browse["reading"])
      ? (stored.reading as Browse["reading"])
      : base.reading,
    rating:
      rating === "all" ||
      rating === "rated" ||
      rating === "unrated" ||
      stars(rating)
        ? (rating as string)
        : base.rating,
    sourceRating:
      sourceRating === "all" || stars(sourceRating)
        ? (sourceRating as string)
        : base.sourceRating,
    order: stored.order === "random" ? "random" : "default",
  };
}

/** Filters belong to this URL; scroll belongs to this history entry. */
export function restoreLibraryView(
  search = location.search,
  state = history.state,
): LibraryView {
  const base = initialBrowse();
  const params = new URLSearchParams(search);
  const stored = Object.fromEntries(params);
  const page = Number(params.get("page"));
  const scroll = state?.mcScroll;
  return {
    browse: {
      ...filtersFrom(stored, base),
      query: stored.query && stored.query.length <= 1024 ? stored.query : "",
      seed:
        stored.seed && /^[0-9a-f]{32}$/.test(stored.seed)
          ? stored.seed
          : base.seed,
    },
    page: Number.isSafeInteger(page) && page >= 1 && page <= 1000000 ? page : 1,
    scroll:
      scroll &&
      typeof scroll.query === "string" &&
      typeof scroll.y === "number" &&
      Number.isFinite(scroll.y)
        ? { query: scroll.query, y: Math.max(0, scroll.y) }
        : null,
  };
}

export function librarySearch(browse: Browse, page = 1): string {
  const params = new URLSearchParams();
  if (browse.query) params.set("query", browse.query);
  for (const key of ["category", "language"] as const)
    if (browse[key]) params.set(key, browse[key]);
  for (const key of ["reading", "rating", "sourceRating"] as const)
    if (browse[key] !== "all") params.set(key, browse[key]);
  if (browse.order === "random") {
    params.set("order", "random");
    params.set("seed", browse.seed);
  }
  if (page > 1) params.set("page", String(page));
  const value = params.toString();
  return value ? `?${value}` : "";
}

/** Update the current library entry without sharing filters across tabs. */
export function rememberLibraryView(view: LibraryView) {
  history.replaceState(
    { ...history.state, mcScroll: view.scroll },
    "",
    location.pathname + librarySearch(view.browse, view.page) + location.hash,
  );
}

/** Where filters were kept before they moved into the URL; nothing reads them now. */
const RETIRED = [
  ["localStorage", storageKey("library-filters")],
  ["sessionStorage", storageKey("library-view")],
] as const;
/** Removes the retired filter keys once at startup; reader settings are untouched. */
export function forgetRetiredLibraryStorage() {
  for (const [area, key] of RETIRED)
    try {
      globalThis[area].removeItem(key);
    } catch {
      /* Storage may be unavailable. */
    }
}
