import { tagQuery } from "./Pills";
import { useEffect, useId, useRef, useState } from "react";
import { Search, X } from "lucide-react";
import { aborted, request } from "./api";
import {
  TagText,
  cleanLabel,
  namespaceLabel,
  storedTag,
  tagLabel,
} from "./Pills";
import type { Translate } from "./types";

export type Suggestion = {
  namespace: string;
  value: string;
  translation: string | null;
  count: number;
};
/** The whitespace-separated tokens of a query; quoted text and escapes stay in one token. */
function tokensOf(value: string) {
  let start = 0,
    quote = "",
    escaped = false;
  const tokens: { start: number; end: number }[] = [];
  for (let i = 0; i <= value.length; i++) {
    const ch = value[i] ?? " ";
    if (escaped) {
      escaped = false;
      continue;
    }
    if (ch === "\\") {
      escaped = true;
      continue;
    }
    if (quote) {
      if (ch === quote) quote = "";
      continue;
    }
    if (ch === '"') {
      quote = ch;
      continue;
    }
    if (/\s/.test(ch)) {
      tokens.push({ start, end: i });
      start = i + 1;
    }
  }
  if (start < value.length) tokens.push({ start, end: value.length });
  return tokens;
}
export function tokenAt(value: string, caret: number) {
  return (
    tokensOf(value).find(
      (token) => token.start <= caret && caret <= token.end,
    ) ?? { start: caret, end: caret }
  );
}
/** How a tag may have been typed: its value or translation, with or without a namespace. */
function typedForms(tag: Suggestion) {
  const namespace = tag.namespace || "other";
  const values = [tag.value, tag.translation && cleanLabel(tag.translation)];
  const prefixes = [
    "",
    `${namespace}:`,
    `${namespaceLabel(namespace, (zh) => zh)}:`,
    `${namespaceLabel(namespace, (_, en) => en)}:`,
  ];
  return values.flatMap((value) =>
    value ? prefixes.map((prefix) => prefix + value) : [],
  );
}
const comparable = (text: string) =>
  text
    .replace(/\\(.)/g, "$1")
    .replace(/"/g, "")
    .replace(/\s+/g, " ")
    .toLowerCase();
/**
 * The text that choosing `tag` replaces: the token at the caret, extended back over the
 * words before it when together they begin the tag as typed (`female:big br` for
 * `female:big breasts`), so no piece of the typed tag stays behind as a separate term.
 */
export function suggestionRange(value: string, caret: number, tag: Suggestion) {
  const token = tokenAt(value, caret);
  const forms = typedForms(tag).map(comparable);
  let start = token.start;
  for (const word of tokensOf(value)) {
    if (word.start >= token.start) break;
    const typed = comparable(value.slice(word.start, token.end));
    if (forms.some((form) => form.startsWith(typed))) {
      start = word.start;
      break;
    }
  }
  return { start, end: token.end };
}
/** Typing pauses this long before the library search runs. */
export const SEARCH_DEBOUNCE_MS = 280;
/** A suggestion lookup waits this long after the caret token changes. */
export const SUGGESTION_DELAY_MS = 120;

export function SearchBox({
  value,
  change,
  search,
  t,
  embedded = false,
}: {
  /** The applied query; the box keeps its own draft while typing. */
  value: string;
  /** Optional: told about every edit of the draft (before the debounced search). */
  change?: (value: string) => void;
  search: (value: string) => void;
  t: Translate;
  embedded?: boolean;
}) {
  const id = useId();
  const input = useRef<HTMLInputElement>(null);
  const composing = useRef(false);
  const timer = useRef<ReturnType<typeof setTimeout> | undefined>(undefined);
  const callbacks = useRef({ change, search });
  callbacks.current = { change, search };
  const emitted = useRef(value);
  const [chips, setChips] = useState<Suggestion[]>([]);
  const [draft, setDraft] = useState(value);
  const [ime, setIme] = useState(false);
  const [caret, setCaret] = useState(value.length);
  const [open, setOpen] = useState(false);
  const [items, setItems] = useState<Suggestion[]>([]);
  const [active, setActive] = useState(-1);
  const [failed, setFailed] = useState(false);
  const [loading, setLoading] = useState(false);
  const token = tokenAt(draft, caret);
  const fragment = draft.slice(token.start, token.end);
  const query = (tags: Suggestion[], text: string) =>
    [...tags.map((tag) => tagQuery(tag.namespace, tag.value)), text]
      .filter(Boolean)
      .join(" ")
      .trim();
  function emit(tags: Suggestion[], text: string, immediate = false) {
    const next = query(tags, text);
    clearTimeout(timer.current);
    if (next.length > 1024) return;
    emitted.current = next;
    callbacks.current.change?.(next);
    clearTimeout(timer.current);
    if (immediate) callbacks.current.search(next);
    else if (!composing.current)
      timer.current = setTimeout(
        () => callbacks.current.search(next),
        SEARCH_DEBOUNCE_MS,
      );
  }
  useEffect(() => {
    if (value !== emitted.current) {
      clearTimeout(timer.current);
      setChips([]);
      setDraft(value);
      setCaret(value.length);
      emitted.current = value;
    }
  }, [value]);
  useEffect(() => () => clearTimeout(timer.current), []);
  useEffect(() => {
    const controller = new AbortController();
    setActive(-1);
    setFailed(false);
    // Suggestions belong to one fragment; never show the previous fragment's list.
    setItems([]);
    if (!open || ime || !fragment.trim()) {
      setLoading(false);
      return;
    }
    setLoading(true);
    const wait = setTimeout(() => {
      void request<{ items: Suggestion[] }>(
        `/api/tag-suggestions?query=${encodeURIComponent(fragment)}&limit=5`,
        { signal: controller.signal },
      )
        .then((result) => {
          if (!controller.signal.aborted) setItems(result.items.slice(0, 5));
        })
        .catch((error) => {
          if (!aborted(error)) setFailed(true);
        })
        .finally(() => {
          if (!controller.signal.aborted) setLoading(false);
        });
    }, SUGGESTION_DELAY_MS);
    return () => {
      clearTimeout(wait);
      controller.abort();
    };
  }, [fragment, open, ime]);
  function choose(item: Suggestion) {
    const next = [...chips, item];
    const range = suggestionRange(draft, caret, item);
    const text =
      `${draft.slice(0, range.start).trimEnd()} ${draft.slice(range.end).trimStart()}`.trim();
    if (query(next, text).length > 1024) return;
    setChips(next);
    setDraft(text);
    setCaret(text.length);
    setOpen(false);
    setActive(-1);
    emit(next, text, true);
    input.current?.focus();
  }
  function remove(index: number) {
    const next = chips.filter((_, i) => i !== index);
    setChips(next);
    emit(next, draft, true);
    input.current?.focus();
  }
  const Container = embedded ? "div" : "form";
  const visible = open && !ime && !!fragment.trim();
  return (
    <Container
      className="search search-combobox"
      onSubmit={(event) => {
        event.preventDefault();
        if (!composing.current) {
          setOpen(false);
          emit(chips, draft, true);
        }
      }}
      onBlur={(event) => {
        if (!event.currentTarget.contains(event.relatedTarget)) setOpen(false);
      }}
    >
      <Search size={18} aria-hidden="true" />
      <div className="search-tokens">
        {chips.map((tag, index) => (
          <button
            key={index}
            type="button"
            className="pill search-token"
            aria-label={tagLabel(tag, tag.translation, t)}
            onClick={(event) => {
              setOpen(false);
              event.currentTarget.focus();
            }}
            onKeyDown={(event) => {
              if (["Delete", "Backspace"].includes(event.key)) {
                event.preventDefault();
                remove(index);
              }
            }}
          >
            <TagText tag={tag} translation={tag.translation} t={t} />
          </button>
        ))}
        <input
          ref={input}
          data-escape-local={visible ? "true" : undefined}
          role="combobox"
          aria-label={t("搜索书库", "Search library")}
          aria-autocomplete="list"
          aria-expanded={visible}
          aria-controls={id}
          aria-activedescendant={
            visible && active >= 0 ? `${id}-${active}` : undefined
          }
          placeholder={
            chips.length
              ? t("继续输入…", "Add search…")
              : t("搜索标题、标签、文件名…", "Search titles, tags, filenames…")
          }
          autoComplete="off"
          maxLength={Math.max(
            0,
            1024 - query(chips, "").length - (chips.length ? 1 : 0),
          )}
          value={draft}
          onClick={() => {
            setCaret(input.current?.selectionStart ?? draft.length);
            setOpen(false);
          }}
          onSelect={() =>
            setCaret(input.current?.selectionStart ?? draft.length)
          }
          onCompositionStart={() => {
            composing.current = true;
            setIme(true);
            clearTimeout(timer.current);
          }}
          onCompositionEnd={(event) => {
            composing.current = false;
            setIme(false);
            emit(chips, event.currentTarget.value);
          }}
          onChange={(event) => {
            const text = event.target.value.slice(0, event.target.maxLength);
            setDraft(text);
            setCaret(event.target.selectionStart ?? event.target.value.length);
            setOpen(true);
            emit(chips, text);
          }}
          onKeyDown={(event) => {
            if (
              composing.current ||
              event.nativeEvent?.isComposing ||
              event.keyCode === 229
            )
              return;
            // Escape closes the suggestions first; with none shown it reaches the page or dialog.
            if (event.key === "Escape" && visible) {
              event.preventDefault();
              event.stopPropagation();
              setOpen(false);
              setActive(-1);
            } else if (
              visible &&
              items.length &&
              ["ArrowDown", "ArrowUp"].includes(event.key)
            ) {
              event.preventDefault();
              setActive((index) =>
                event.key === "ArrowDown"
                  ? (index + 1) % items.length
                  : index <= 0
                    ? items.length - 1
                    : index - 1,
              );
            } else if (
              visible &&
              active >= 0 &&
              ["Enter", "Tab"].includes(event.key)
            ) {
              event.preventDefault();
              choose(items[active]!);
            } else if (embedded && event.key === "Enter") {
              event.preventDefault();
              setOpen(false);
              emit(chips, draft, true);
            }
          }}
        />
      </div>
      {(draft || chips.length > 0) && (
        <button
          type="button"
          className="icon-button close-button search-clear"
          aria-label={t("清空搜索", "Clear search")}
          onClick={() => {
            setChips([]);
            setDraft("");
            setCaret(0);
            setOpen(false);
            emit([], "", true);
            input.current?.focus();
          }}
        >
          <X size={14} />
        </button>
      )}
      {visible && (
        <div className="suggestion-popover" aria-busy={loading}>
          <ul
            role="listbox"
            id={id}
            aria-label={t("标签建议", "Tag suggestions")}
          >
            {items.map((item, index) => (
              <li
                key={`${item.namespace}:${item.value}`}
                id={`${id}-${index}`}
                role="option"
                aria-selected={index === active}
                onPointerDown={(event) => event.preventDefault()}
                onMouseEnter={() => setActive(index)}
                onClick={() => choose(item)}
              >
                <span>
                  <strong>
                    <TagText tag={item} translation={item.translation} t={t} />
                    <span className="tag-count">{item.count}</span>
                  </strong>
                  {/* While typing, the stored English shows too: what was typed may only be there. */}
                  {tagLabel(item, item.translation, t) !== storedTag(item) && (
                    <small>{storedTag(item)}</small>
                  )}
                </span>
              </li>
            ))}
          </ul>
          {!items.length && (
            <p className="muted small">
              {failed
                ? t("建议暂不可用，仍可直接搜索", "Suggestions unavailable")
                : loading
                  ? t("正在查找…", "Searching…")
                  : t("没有匹配标签", "No matching tags")}
            </p>
          )}
          <div className="suggestion-caption">
            <span className="pill">↑↓ {t("选词", "Choose")}</span>
            <span className="pill">Enter / Tab</span>
            <span className="pill">Esc {t("关闭", "Close")}</span>
          </div>
        </div>
      )}
    </Container>
  );
}
