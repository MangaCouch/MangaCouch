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
/** Display names of the tag namespaces (the server accepts them in searches, see NAMESPACE_ALIASES); unknown namespaces show as stored. */
export const namespaceNames: Record<string, string> = {
  artist: "作者",
  group: "社团",
  parody: "原作",
  character: "角色",
  female: "女性",
  male: "男性",
  mixed: "混合",
  other: "其他",
  language: "语言",
  cosplayer: "角色扮演者",
  reclass: "类别",
  category: "分类",
  location: "地点",
  temp: "临时",
  tag: "标签",
};
/** Other translated names the server also accepts (EhTagTranslation's, see SEARCH_NAMESPACE_ALIASES). */
const namespaceAliases: Record<string, string> = {
  艺术家: "artist",
  团队: "group",
  重新分类: "reclass",
  coser: "cosplayer",
};
/** The stored key of a namespace typed by its key or its translated name ("作者" is "artist"). */
export function namespaceKey(text: string) {
  const name = text.trim();
  const translated = Object.entries(namespaceNames).find(
    ([, label]) => label === name,
  )?.[0];
  return (
    translated ?? namespaceAliases[name.toLowerCase()] ?? (name || "other")
  );
}
export function namespaceLabel(namespace: string, t: Translate) {
  const key = namespace || "other";
  return t(namespaceNames[key] ?? key, key);
}
/** Icons (Markdown images) and markup that translated names may carry; emoji stay. */
const LABEL_MARKUP = /!\[[^\]]*\]\([^)]*\)|<[^>]*>/g;
/** A translated name as shown; the server cleans imported names the same way. */
export const cleanLabel = (text: string) =>
  text.replace(LABEL_MARKUP, "").replace(/\s+/g, " ").trim();
/** The shown text of a tag value: its translation, else the stored English value. */
export const valueLabel = (tag: Tag, translation?: string | null) =>
  (translation && cleanLabel(translation)) || tag.value;
/** The stored English form of a tag, shown on hover. */
export const storedTag = (tag: Tag) =>
  `${tag.namespace || "other"}:${tag.value}`;
/** A namespace by its translated name; hovering shows the stored English one. */
export function NamespaceName({
  namespace,
  t,
}: {
  namespace: string;
  t: Translate;
}) {
  const key = namespace || "other";
  const label = namespaceLabel(key, t);
  return <span title={label === key ? undefined : key}>{label}</span>;
}
/** A tag as "作者:蓝天" (it is still stored in English, shown on hover). */
export function TagText({
  tag,
  translation,
  t,
}: {
  tag: Tag;
  translation?: string | null;
  t: Translate;
}) {
  return (
    <span className="tag-text" title={storedTag(tag)}>
      {tagLabel(tag, translation, t)}
    </span>
  );
}
/** Plain-text form of TagText. */
export function tagLabel(
  tag: Tag,
  translation: string | null | undefined,
  t: Translate,
) {
  return `${namespaceLabel(tag.namespace, t)}:${valueLabel(tag, translation)}`;
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
  const label = valueLabel(tag, translation);
  const body = (
    <>
      {label}
      {count !== undefined && <span className="tag-count">{count}</span>}
    </>
  );
  // Hovering a translated tag shows the stored English one.
  const title = label === tag.value ? undefined : storedTag(tag);
  return click ? (
    <button
      type="button"
      className="pill tag-pill"
      title={title}
      onClick={click}
    >
      {body}
    </button>
  ) : (
    <span className="pill tag-pill" title={title}>
      {body}
    </span>
  );
}

/** Preserve custom namespace identity, including spaces, colons and quotes. */
export function tagQuery(namespace: string, value: string): string {
  const key = namespace || "other";
  return `${/[\s:"\\：]/.test(key) ? JSON.stringify(key) : key}:${JSON.stringify(value)}`;
}
