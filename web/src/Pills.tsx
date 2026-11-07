import type { Book, Tag, Translate } from "./types";
import type { ReactNode } from "react";

export const languages = ["English", "Japanese", "Chinese", "Other", "Unknown"];
export const workTypes = [
  "Doujinshi",
  "Manga",
  "Artist CG",
  "Game CG",
  "Western",
  "Non-H",
  "Image Set",
  "Cosplay",
  "Asian Porn",
  "Misc",
];
export function categoryLabel(value: string, t: Translate) {
  const names: Record<string, string> = {
    Doujinshi: "同人志",
    Manga: "漫画",
    "Artist CG": "画师CG",
    "Game CG": "游戏CG",
    Western: "西方",
    "Non-H": "无H",
    "Image Set": "图集",
    Misc: "杂项",
    Cosplay: "角色扮演",
    "Asian Porn": "亚洲成人",
  };
  const canonical =
    workTypes.find((type) => type.toLowerCase() === value.toLowerCase()) ??
    value;
  return t(names[canonical] ?? value, canonical);
}
export function languageLabel(value: unknown, t: Translate) {
  const language = languageName(value);
  const names: Record<string, string> = {
    Chinese: "中文",
    Japanese: "日文",
    English: "英文",
    Other: "其他",
    Unknown: "未知",
  };
  return t(names[language]!, language);
}
export function languageName(value: unknown) {
  const text = String(value ?? "").toLowerCase();
  if (["en", "english", "eng", "英文", "英语"].includes(text)) return "English";
  if (["ja", "jp", "japanese", "日语", "日文", "日本語"].includes(text))
    return "Japanese";
  if (["cn", "zh", "chinese", "中文", "汉语"].includes(text)) return "Chinese";
  return !text || ["unknown", "未知"].includes(text) ? "Unknown" : "Other";
}
export function Pill({
  kind = "plain",
  value,
  children,
}: {
  kind?: string;
  value?: unknown;
  children: ReactNode;
}) {
  return (
    <span
      className={`pill pill-${kind}`}
      data-value={String(value ?? "")
        .toLowerCase()
        .replace(/[^a-z0-9]+/g, "-")}
    >
      {children}
    </span>
  );
}
export function BookPills({
  book,
  t,
  file = false,
  rating = true,
}: {
  book: Book;
  t: Translate;
  file?: boolean;
  rating?: boolean;
}) {
  const language = languageName(book.metadata.language);
  const score = book.personal.rating ?? book.metadata.source_rating?.average;
  const work = book.metadata.work_type ?? "";
  return (
    <span className="book-pills">
      {work && (
        <Pill kind="category" value={work}>
          {categoryLabel(work, t)}
        </Pill>
      )}
      <Pill kind="language" value={language}>
        {languageLabel(language, t)}
      </Pill>
      {file && (
        <Pill kind="file" value={book.content.format}>
          {book.content.format.toUpperCase()}
        </Pill>
      )}
      <Pill kind="pages">
        {book.content.page_count}
        {t("页", " pages")}
      </Pill>
      {rating && score != null && (
        <Pill kind="rating">
          {book.personal.rating != null ? (
            <b>{score} ★</b>
          ) : (
            <span>{score} ★</span>
          )}
        </Pill>
      )}
    </span>
  );
}
export function TagPill({
  tag,
  translation,
  count,
  click,
}: {
  tag: Tag;
  translation?: string | null;
  count?: number;
  click?: () => void;
}) {
  const body = (
    <>
      {translation || tag.value}
      {translation && translation !== tag.value && <small>{tag.value}</small>}
      {count !== undefined && <span className="tag-count">{count}</span>}
    </>
  );
  return click ? (
    <button type="button" className="pill tag-pill" onClick={click}>
      {body}
    </button>
  ) : (
    <span className="pill tag-pill">{body}</span>
  );
}
