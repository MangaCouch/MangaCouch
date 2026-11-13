import { useEffect, useId, useState, useRef } from "react";
import { X } from "lucide-react";
import { aborted, request } from "./api";
import {
  NamespaceName,
  TagPill,
  namespaceKey,
  storedTag,
  valueLabel,
} from "./Pills";
import type { Suggestion } from "./SearchBox";
import type { Tag, Translate } from "./types";

/** Text typed into the tag field but not yet added with Enter. */
export type TagDraft = { tags: Tag[]; replace: number | null };

/** `tags` with `added` in place of the tag at `replace` (or appended), without duplicates. */
export function withTags(tags: Tag[], added: Tag[], replace: number | null) {
  const next = [...tags];
  if (replace !== null) next.splice(replace, 1, ...added);
  else next.push(...added);
  return next.filter(
    (tag, index, all) =>
      all.findIndex(
        (item) => item.namespace === tag.namespace && item.value === tag.value,
      ) === index,
  );
}

/**
 * The tag a half-typed entry stands for: the value after a chosen namespace, or text
 * without one as an "other" tag (as pasting does). Renaming a tag's namespace, which has
 * no value yet, stands for no change.
 */
function draftTag(
  namespace: string | null,
  draft: string,
  editing: number | null,
): TagDraft | null {
  const text = draft.trim();
  if (!text || (!namespace && editing !== null)) return null;
  return {
    tags: [{ namespace: namespace ?? "other", value: text }],
    replace: editing,
  };
}

export function TagEditor({
  value,
  change,
  pending,
  t,
}: {
  value: Tag[];
  change: (tags: Tag[]) => void;
  /** Reports the entry not yet added, so saving includes it rather than dropping it. */
  pending?: (draft: TagDraft | null) => void;
  t: Translate;
}) {
  const id = useId();
  const input = useRef<HTMLInputElement>(null);
  const tagButtons = useRef<(HTMLButtonElement | null)[]>([]);
  const [namespace, setNamespace] = useState<string | null>(null);
  const [draft, setDraft] = useState("");
  const [editing, setEditing] = useState<number | null>(null);
  const [items, setItems] = useState<Suggestion[]>([]);
  const [active, setActive] = useState(-1);
  const [focus, setFocus] = useState(false);
  const [ime, setIme] = useState(false);
  const [invalidNamespace, setInvalidNamespace] = useState(false);
  const namespaceError = t(
    "标签命名空间最多 128 个字符。",
    "Tag namespaces allow at most 128 characters.",
  );
  useEffect(() => {
    input.current?.setCustomValidity(invalidNamespace ? namespaceError : "");
  }, [invalidNamespace, namespaceError]);
  const validNamespace = (value: string) => {
    const valid = [...value].length <= 128;
    setInvalidNamespace(!valid);
    return valid;
  };
  const report = useRef(pending);
  report.current = pending;
  const entry = invalidNamespace ? null : draftTag(namespace, draft, editing);
  const original = editing !== null ? value[editing] : undefined;
  // Opening a tag for editing without changing it leaves nothing unsent.
  const unsent = JSON.stringify(
    entry &&
      original &&
      entry.tags[0]?.namespace === (original.namespace || "other") &&
      entry.tags[0]?.value === original.value
      ? null
      : entry,
  );
  useEffect(() => {
    report.current?.(JSON.parse(unsent) as TagDraft | null);
  }, [unsent]);
  useEffect(() => {
    setActive(-1);
    setItems([]);
    if (!draft.trim() || ime || invalidNamespace) return;
    const controller = new AbortController();
    const timer = setTimeout(() => {
      void request<{ items: Suggestion[] }>(
        `/api/tag-suggestions?query=${encodeURIComponent((namespace ? namespace + ":" : "") + draft)}&limit=5&database=true`,
        { signal: controller.signal },
      )
        .then((data) => {
          if (!controller.signal.aborted)
            setItems(
              data.items.filter(
                (tag) => !namespace || tag.namespace === namespace,
              ),
            );
        })
        .catch((error) => {
          if (!aborted(error)) setItems([]);
        });
    }, 120);
    return () => {
      clearTimeout(timer);
      controller.abort();
    };
  }, [namespace, draft, ime, invalidNamespace]);
  const suggestions = items;
  function commit(text: string) {
    if (invalidNamespace) return;
    if (!namespace && editing !== null) {
      const key = namespaceKey(text.trim().replace(/:$/, ""));
      if (!validNamespace(key)) return;
      setNamespace(key);
      setDraft(value[editing]?.value ?? "");
      return;
    }
    if (!text.trim()) return;
    add([{ namespace: namespace ?? "other", value: text.trim() }]);
  }
  /** Adds tags (the first replaces the one being edited) and resets the entry. */
  function add(added: Tag[]) {
    if (added.some((tag) => !validNamespace(tag.namespace))) return;
    change(withTags(value, added, editing));
    setNamespace(null);
    setDraft("");
    setEditing(null);
    setActive(-1);
    setFocus(false);
  }
  /** Loads tag `index` into the entry: its value, or with `part` "namespace" its namespace. */
  function edit(index: number, part: "value" | "namespace" = "value") {
    const tag = value[index];
    if (!tag) return;
    setInvalidNamespace(false);
    setEditing(index);
    setNamespace(part === "value" ? tag.namespace || "other" : null);
    setDraft(part === "value" ? tag.value : tag.namespace || "other");
    setFocus(true);
    input.current?.focus();
  }
  /** Leaves the tag being edited as it was and returns focus to it. */
  function cancelEdit() {
    const index = editing;
    setInvalidNamespace(false);
    setEditing(null);
    setNamespace(null);
    setDraft("");
    setFocus(false);
    if (index !== null) tagButtons.current[index]?.focus();
  }
  function remove(index: number) {
    change(value.filter((_, i) => i !== index));
    if (editing === index) {
      setInvalidNamespace(false);
      setEditing(null);
      setDraft("");
      setNamespace(null);
    } else if (editing !== null && editing > index) setEditing(editing - 1);
  }
  return (
    <div
      className="tag-editor"
      onBlur={(event) => {
        if (!event.currentTarget.contains(event.relatedTarget)) setFocus(false);
      }}
    >
      <div className="tags">
        {value.map((tag, index) => (
          <span
            className={`editable-tag ${editing === index ? "editing" : ""}`}
            key={index}
          >
            {/* A click, tap, Enter or F2 edits the value; a double click on the namespace
                edits the namespace; Delete or Backspace removes the tag. */}
            <button
              type="button"
              className="tag-edit"
              ref={(node) => {
                tagButtons.current[index] = node;
              }}
              aria-label={t(
                `编辑标签 ${storedTag(tag)}`,
                `Edit tag ${storedTag(tag)}`,
              )}
              title={t(
                "点按编辑，Delete 移除",
                "Click to edit, Delete to remove",
              )}
              onClick={() => edit(index)}
              onKeyDown={(event) => {
                if (event.key === "F2") {
                  event.preventDefault();
                  edit(index);
                } else if (
                  event.key === "Delete" ||
                  event.key === "Backspace"
                ) {
                  event.preventDefault();
                  remove(index);
                  // Focus moves to the tag that takes this one's place, or the entry.
                  const next = Math.min(index, value.length - 2);
                  setTimeout(() =>
                    (next >= 0
                      ? tagButtons.current[next]
                      : input.current
                    )?.focus(),
                  );
                } else if (event.key === "Escape" && editing === index) {
                  event.preventDefault();
                  event.stopPropagation();
                  cancelEdit();
                }
              }}
            >
              <strong
                onDoubleClick={(event) => {
                  event.stopPropagation();
                  edit(index, "namespace");
                }}
              >
                <NamespaceName namespace={tag.namespace} t={t} />:
              </strong>
              <TagPill tag={tag} />
            </button>
            <button
              type="button"
              className="icon-button"
              aria-label={t(`移除 ${tag.value}`, `Remove ${tag.value}`)}
              onClick={() => remove(index)}
            >
              <X size={12} />
            </button>
          </span>
        ))}
      </div>
      <div className="tag-entry">
        {invalidNamespace && (
          <p role="alert" className="small error-text">
            {namespaceError}
          </p>
        )}
        {namespace && (
          <button
            type="button"
            className="pill"
            onClick={() => {
              setNamespace(null);
              setDraft("");
            }}
          >
            <NamespaceName namespace={namespace} t={t} />:
          </button>
        )}
        <input
          ref={input}
          data-escape-local={namespace || draft ? "true" : undefined}
          role="combobox"
          aria-label={namespace ? t("标签值", "Tag value") : t("标签", "Tag")}
          aria-expanded={focus && suggestions.length > 0}
          aria-controls={id}
          aria-activedescendant={active >= 0 ? `${id}-${active}` : undefined}
          value={draft}
          maxLength={!namespace && editing !== null ? 128 : 512}
          placeholder={
            namespace ? t("输入标签…", "Tag…") : t("添加标签…", "Add tag…")
          }
          onFocus={() => setFocus(true)}
          onCompositionStart={() => setIme(true)}
          onCompositionEnd={() => setIme(false)}
          onChange={(event) => {
            const text = event.target.value;
            setInvalidNamespace(false);
            if (!namespace && text.includes(":")) {
              const parts = text.split(":");
              const key = namespaceKey(parts[0] ?? "");
              if (!validNamespace(key)) {
                setDraft(text);
                return;
              }
              setNamespace(key);
              const rest = parts.slice(1).join(":");
              // Renaming a tag's namespace ("group:") keeps its value, as Enter does.
              setDraft(
                editing !== null && !rest
                  ? (value[editing]?.value ?? "")
                  : rest,
              );
            } else setDraft(text);
            setFocus(true);
          }}
          onPaste={(event) => {
            // "artist:a, group:b" (one tag per comma or line) adds every tag at once.
            const text = event.clipboardData.getData("text");
            const parts = text.split(/[,\n]/).map((part) => part.trim());
            if (parts.filter(Boolean).length < 2) return;
            event.preventDefault();
            const tags = parts.flatMap((part) => {
              const separator = part.indexOf(":");
              const tag =
                separator >= 0
                  ? {
                      namespace: namespaceKey(part.slice(0, separator)),
                      value: part.slice(separator + 1).trim(),
                    }
                  : { namespace: namespace ?? "other", value: part };
              return tag.value ? [tag] : [];
            });
            if (tags.length) add(tags);
          }}
          onKeyDown={(event) => {
            if (ime || event.nativeEvent.isComposing || event.keyCode === 229)
              return;
            if (
              ["ArrowDown", "ArrowUp"].includes(event.key) &&
              suggestions.length
            ) {
              event.preventDefault();
              setActive((index) =>
                event.key === "ArrowDown"
                  ? (index + 1) % suggestions.length
                  : index <= 0
                    ? suggestions.length - 1
                    : index - 1,
              );
            } else if (event.key === "Enter") {
              event.preventDefault();
              if (active >= 0 && suggestions[active]) {
                const item = suggestions[active]!;
                add([{ namespace: item.namespace, value: item.value }]);
              } else commit(draft);
            } else if (event.key === "Escape") {
              event.preventDefault();
              event.stopPropagation();
              setInvalidNamespace(false);
              // Editing an existing tag: one Escape leaves it as it was.
              if (editing !== null) cancelEdit();
              else if (namespace && draft) setDraft("");
              else if (namespace) {
                setNamespace(null);
                setDraft("");
              } else {
                setEditing(null);
                setDraft("");
                setFocus(false);
              }
            } else if (event.key === "Backspace" && !draft && namespace) {
              event.preventDefault();
              setNamespace(null);
            }
          }}
        />
      </div>
      {focus && suggestions.length > 0 && (
        <ul className="tag-editor-suggestions" id={id} role="listbox">
          {suggestions.map((item, index) => (
            <li
              key={`${item.namespace}:${item.value}`}
              role="option"
              aria-selected={active === index}
              id={`${id}-${index}`}
              onPointerDown={(event) => event.preventDefault()}
              onMouseEnter={() => setActive(index)}
              onClick={() =>
                add([{ namespace: item.namespace, value: item.value }])
              }
              title={storedTag(item)}
            >
              <strong>
                <NamespaceName namespace={item.namespace} t={t} />:
                {valueLabel(item, item.translation)}
              </strong>
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}
