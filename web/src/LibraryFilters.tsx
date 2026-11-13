import { Select, Segmented } from "./Controls";
import { Rating } from "./Rating";
import { languages, workTypes, categoryLabel, languageLabel } from "./Pills";
import { useEffect, useRef } from "react";
import { SlidersHorizontal, X } from "lucide-react";
import type { Translate } from "./types";

export interface Browse {
  query: string;
  category?: string;
  language?: string;
  reading: "all" | "unread" | "in_progress" | "completed";
  rating: string;
  sourceRating: string;
  order: "default" | "random";
  seed: string;
}
const newSeed = () =>
  Array.from(crypto.getRandomValues(new Uint8Array(16)), (byte) =>
    byte.toString(16).padStart(2, "0"),
  ).join("");
export const initialBrowse = (): Browse => ({
  query: "",
  reading: "all",
  rating: "all",
  sourceRating: "all",
  order: "default",
  seed: newSeed(),
});
/** Books per library page (shelf, pagination and keyboard paging). */
export const PAGE_SIZE = 24;
export function searchParameters(browse: Browse) {
  const params = new URLSearchParams({
    limit: String(PAGE_SIZE),
    query: browse.query,
  });
  for (const [key, value] of [
    ["work_type", browse.category],
    ["language", browse.language],
  ])
    if (value) params.set(key!, value);
  params.set("reading", browse.reading);
  const threshold = !["all", "rated", "unrated"].includes(browse.rating);
  params.set("rating", threshold ? "rated" : browse.rating);
  if (threshold) params.set("min_rating", browse.rating);
  if (browse.sourceRating !== "all")
    params.set("source_rating_min", browse.sourceRating);
  params.set("order", browse.order);
  if (browse.order === "random") params.set("seed", browse.seed);
  return params.toString();
}
export function searchOptions(browse: Browse) {
  const options: Record<string, unknown> = {};
  for (const [key, value] of new URLSearchParams(searchParameters(browse)))
    if (!["query", "limit"].includes(key))
      options[key] = ["min_rating", "source_rating_min"].includes(key)
        ? Number(value)
        : value;
  return options;
}
export const hasFilters = (value: Browse) =>
  !!value.category ||
  !!value.language ||
  value.reading !== "all" ||
  value.rating !== "all" ||
  value.sourceRating !== "all";

export function LibraryFilters({
  value,
  change,
  t,
}: {
  value: Browse;
  change: (value: Browse) => void;
  t: Translate;
}) {
  const active = hasFilters(value);
  /** The pressed "my rating" segment; a threshold counts as rated. */
  const ratingChoice = ["all", "unrated"].includes(value.rating)
    ? value.rating
    : "rated";
  const ref = useRef<HTMLDetailsElement>(null);
  useEffect(() => {
    const outside = (event: PointerEvent) => {
      const control = ref.current;
      // A select list that is open belongs to the popover: a press inside it, or the
      // press that closes it (which lands on <html> while the list disables pointer
      // events elsewhere), must not close the filters too.
      if (
        document.querySelector("[data-radix-popper-content-wrapper]") ||
        (event.target as HTMLElement).closest?.(
          "[data-radix-popper-content-wrapper]",
        )
      )
        return;
      if (control?.open && !control.contains(event.target as Node))
        control.open = false;
    };
    const escape = (event: KeyboardEvent) => {
      if (
        !event.defaultPrevented &&
        event.key === "Escape" &&
        ref.current?.open
      ) {
        // Escape only closes the filters; the shelf keeps its selection.
        event.preventDefault();
        ref.current.open = false;
        ref.current.querySelector("summary")?.focus();
      }
    };
    document.addEventListener("pointerdown", outside);
    document.addEventListener("keydown", escape);
    return () => {
      document.removeEventListener("pointerdown", outside);
      document.removeEventListener("keydown", escape);
    };
  }, []);
  return (
    <details
      ref={ref}
      className={`library-filters ${active || value.order === "random" ? "filters-active" : ""}`}
    >
      <summary
        aria-label={t("筛选与排序", "Filter & sort")}
        title={t("筛选与排序", "Filter & sort")}
      >
        <SlidersHorizontal size={20} />
        <span>{t("筛选", "Filter")}</span>
        {(active || value.order === "random") && (
          <i aria-label={t("已筛选", "Filters active")} />
        )}
      </summary>
      <div className="filter-popover glass">
        <header>
          <h2>{t("筛选与排序", "Filter & sort")}</h2>
          <button
            type="button"
            className="icon-button close-button"
            aria-label={t("关闭筛选", "Close filters")}
            onClick={() => {
              if (ref.current) {
                ref.current.open = false;
                ref.current.querySelector("summary")?.focus();
              }
            }}
          >
            <X size={16} />
          </button>
        </header>
        <div className="library-filter-fields">
          <label>
            {t("类别", "Category")}
            <Select
              aria-label={t("类别", "Category")}
              value={value.category ?? ""}
              onChange={(e) => change({ ...value, category: e.target.value })}
            >
              <option value="">{t("全部", "All")}</option>
              {workTypes.map((type) => (
                <option value={type} key={type}>
                  {categoryLabel(type, t)}
                </option>
              ))}
            </Select>
          </label>
          <label>
            {t("语言", "Language")}
            <Select
              aria-label={t("语言", "Language")}
              value={value.language ?? ""}
              onChange={(e) => change({ ...value, language: e.target.value })}
            >
              <option value="">{t("全部", "All")}</option>
              {languages.map((lang) => (
                <option key={lang} value={lang.toLowerCase()}>
                  {languageLabel(lang, t)}
                </option>
              ))}
            </Select>
          </label>
          <div>
            <span>{t("阅读状态", "Reading status")}</span>
            <Segmented
              label={t("阅读状态", "Reading status")}
              value={value.reading}
              change={(v) =>
                change({
                  ...value,
                  // Pressing the chosen state again lifts it: nothing pressed means all.
                  reading: (v === value.reading
                    ? "all"
                    : v) as Browse["reading"],
                })
              }
              options={[
                { value: "unread", label: t("未读", "Unread") },
                { value: "in_progress", label: t("阅读中", "Reading") },
                { value: "completed", label: t("已读完", "Finished") },
              ]}
            />
          </div>
          <div className="filter-source-rating">
            <span>{t("最低来源评分", "Minimum source rating")}</span>
            <Rating
              label={t("最低来源评分", "Minimum source rating")}
              value={
                value.sourceRating === "all" ? null : Number(value.sourceRating)
              }
              change={(v) =>
                change({
                  ...value,
                  sourceRating: v === null ? "all" : String(v),
                })
              }
              t={t}
            />
          </div>
          <div>
            <span>{t("我的评分", "My rating")}</span>
            <Segmented
              label={t("我的评分", "My rating")}
              value={ratingChoice}
              change={(rating) =>
                change({
                  ...value,
                  rating: rating === ratingChoice ? "all" : rating,
                })
              }
              options={[
                { value: "unrated", label: t("未评分", "Unrated") },
                { value: "rated", label: t("已评分", "Rated") },
              ]}
            />
            {!["all", "unrated"].includes(value.rating) && (
              <Rating
                value={value.rating === "rated" ? null : Number(value.rating)}
                change={(v) =>
                  change({ ...value, rating: v === null ? "rated" : String(v) })
                }
                t={t}
              />
            )}
          </div>
          <div>
            <span>{t("排列顺序", "Order")}</span>
            <Segmented
              label={t("排列顺序", "Order")}
              value={value.order}
              change={(v) => change({ ...value, order: v as Browse["order"] })}
              options={[
                { value: "default", label: t("默认", "Default") },
                { value: "random", label: t("随机", "Random") },
              ]}
            />
          </div>
        </div>
        <div className="actions">
          <button
            type="button"
            disabled={!active && value.order === "default"}
            onClick={() =>
              change({
                ...value,
                category: "",
                language: "",
                reading: "all",
                rating: "all",
                sourceRating: "all",
                order: "default",
              })
            }
          >
            {t("清除筛选", "Clear filters")}
          </button>
          {value.order === "random" && (
            <button
              type="button"
              className="subtle"
              onClick={() => change({ ...value, seed: newSeed() })}
            >
              {t("重新打乱", "Shuffle again")}
            </button>
          )}
        </div>
      </div>
    </details>
  );
}
