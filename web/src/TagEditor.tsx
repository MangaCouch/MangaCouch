import { useEffect, useId, useState, useRef } from "react";
import { X } from "lucide-react";
import { aborted, request } from "./api";
import { TagPill } from "./Pills";
import type { Suggestion } from "./SearchBox";
import type { Tag, Translate } from "./types";
const namespaces: Record<string, string> = {
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
};
export function TagEditor({
  value,
  change,
  t,
}: {
  value: Tag[];
  change: (tags: Tag[]) => void;
  t: Translate;
}) {
  const id = useId();
  const input = useRef<HTMLInputElement>(null);
  const [namespace, setNamespace] = useState<string | null>(null);
  const [draft, setDraft] = useState("");
  const [editing, setEditing] = useState<number | null>(null);
  const [items, setItems] = useState<Suggestion[]>([]);
  const [active, setActive] = useState(-1);
  const [focus, setFocus] = useState(false);
  const [ime, setIme] = useState(false);
  useEffect(() => {
    setActive(-1);
    if (!namespace) return;
    const controller = new AbortController();
    const timer = setTimeout(() => {
      void request<{ tags: Suggestion[] }>(
        `/api/tag-suggestions?query=${encodeURIComponent(namespace + ":" + draft)}&limit=5&database=true`,
        { signal: controller.signal },
      )
        .then((data) => {
          if (!controller.signal.aborted)
            setItems(data.tags.filter((tag) => tag.namespace === namespace));
        })
        .catch((error) => {
          if (!aborted(error)) setItems([]);
        });
    }, 120);
    return () => {
      clearTimeout(timer);
      controller.abort();
    };
  }, [namespace, draft]);
  const suggestions = namespace
    ? items
    : Object.entries(namespaces)
        .filter(
          ([key, label]) =>
            key.includes(draft.toLowerCase()) || label.includes(draft),
        )
        .slice(0, 5)
        .map(([key, label]) => ({
          namespace: key,
          value: key,
          translation: label,
          count: 0,
        }));
  function commit(text: string) {
    if (!namespace) {
      setNamespace(text.trim().replace(/:$/, "") || "other");
      setDraft("");
      return;
    }
    if (!text.trim()) return;
    const tag = { namespace, value: text.trim() };
    const tags = [...value];
    if (editing !== null) tags[editing] = tag;
    else tags.push(tag);
    change(
      tags.filter(
        (tag, index, all) =>
          all.findIndex(
            (item) =>
              item.namespace === tag.namespace && item.value === tag.value,
          ) === index,
      ),
    );
    setNamespace(null);
    setDraft("");
    setEditing(null);
    setActive(-1);
    setFocus(false);
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
            onDoubleClick={() => {
              setEditing(index);
              setNamespace(tag.namespace || "other");
              setDraft(tag.value);
              setFocus(true);
              input.current?.focus();
            }}
          >
            <strong
              onDoubleClick={(event) => {
                event.stopPropagation();
                setEditing(index);
                setNamespace(null);
                setDraft(tag.namespace || "other");
                setFocus(true);
                input.current?.focus();
              }}
            >
              {tag.namespace || "other"}:
            </strong>
            <TagPill tag={tag} />
            <button
              type="button"
              className="icon-button"
              aria-label={t(`移除 ${tag.value}`, `Remove ${tag.value}`)}
              onClick={() => {
                change(value.filter((_, i) => i !== index));
                if (editing === index) {
                  setEditing(null);
                  setDraft("");
                  setNamespace(null);
                } else if (editing !== null && editing > index)
                  setEditing(editing - 1);
              }}
            >
              <X size={12} />
            </button>
          </span>
        ))}
      </div>
      <div className="tag-entry">
        {namespace && (
          <button
            type="button"
            className="pill"
            onClick={() => {
              setNamespace(null);
              setDraft("");
            }}
          >
            {namespace}:
          </button>
        )}
        <input
          ref={input}
          data-escape-local={namespace || draft ? "true" : undefined}
          role="combobox"
          aria-label={
            namespace
              ? t("标签值", "Tag value")
              : t("标签命名空间", "Tag namespace")
          }
          aria-expanded={focus && suggestions.length > 0}
          aria-controls={id}
          aria-activedescendant={active >= 0 ? `${id}-${active}` : undefined}
          value={draft}
          maxLength={namespace ? 512 : 128}
          placeholder={
            namespace ? t("输入标签…", "Tag…") : t("添加标签…", "Add tag…")
          }
          onFocus={() => setFocus(true)}
          onCompositionStart={() => setIme(true)}
          onCompositionEnd={() => setIme(false)}
          onChange={(event) => {
            const text = event.target.value;
            if (!namespace && text.includes(":")) {
              const parts = text.split(":");
              setNamespace(parts[0]?.trim() || "other");
              setDraft(parts.slice(1).join(":"));
            } else setDraft(text);
            setFocus(true);
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
              commit(
                active >= 0 && suggestions[active]
                  ? suggestions[active]!.value
                  : draft,
              );
            } else if (event.key === "Escape") {
              event.preventDefault();
              event.stopPropagation();
              if (namespace && draft) setDraft("");
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
              onClick={() => commit(item.value)}
            >
              <strong>{item.translation || item.value}</strong>
              <small>{item.value}</small>
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}
