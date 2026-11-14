import { useState } from "react";
import { Pill, TagText } from "./Pills";
import { exactBytes } from "./format";
import type {
  Conflict,
  Metadata,
  ReviewPreview,
  SidecarSync,
  Translate,
} from "./types";

const fieldNames: Record<string, [string, string]> = {
  title: ["标题", "Title"],
  original_title: ["原始标题", "Original title"],
  language: ["语言", "Language"],
  translated: ["翻译标记", "Translated"],
  work_type: ["作品类型", "Type"],
  tags: ["标签", "Tags"],
  source: ["来源", "Source"],
  uploader: ["上传者", "Uploader"],
  uploaded_at: ["上传时间", "Upload date"],
  source_rating: ["来源评分", "Source rating"],
  declared_page_count: ["来源页数", "Source pages"],
  declared_byte_size: ["来源大小", "Source size"],
  parent: ["父版本", "Parent version"],
  newer_versions: ["新版", "Newer versions"],
};
const issueNames: Record<string, [string, string]> = {
  local_confirmation_missing: [
    "没有与当前文件和全部字段匹配的本地确认记录",
    "No local confirmation matches this file and all current fields",
  ],
  page_manifest_missing: ["缺少有效页面清单", "A valid page list is missing"],
  archive_unavailable: [
    "原始文件不可用或内容已改变",
    "The archive is unavailable or has changed",
  ],
  title_missing: [
    "标题为空，使用原文件名",
    "No title; use the original filename",
  ],
  language_unknown: ["语言未知", "Language is unknown"],
  source_absent: ["没有来源", "No source"],
  source_unverified: [
    "尚未核实来源与本文件的对应关系",
    "Source-to-file match is unverified",
  ],
  declared_page_count_mismatch: [
    "来源页数与实际页数不符",
    "Source and actual page counts differ",
  ],
  declared_byte_size_mismatch: [
    "来源声明大小与实际大小不符",
    "Source and actual sizes differ",
  ],
  candidates_unresolved: [
    "仍有待选择或跳过的候选",
    "There are unresolved candidates",
  ],
  sidecar_conflicts: [
    "外部随书信息文件有冲突",
    "The external metadata file has conflicts",
  ],
};
export const label = (field: string, t: Translate) =>
  fieldNames[field] ? t(...fieldNames[field]) : field;
const importName = (name: string, t: Translate) =>
  name.startsWith("online:")
    ? {
        "online:ehentai": "E-Hentai",
        "online:exhentai": "ExHentai",
      }[name] || name
    : name === "native"
      ? t("Native 元数据", "Native metadata")
      : name === "eze:full"
        ? t("Eze 完整信息", "Eze full metadata")
        : name === "eze:info"
          ? t("Eze 基本信息", "Eze basic metadata")
          : t("其他候选", "Other candidate");

/** What the actual size in a size check measures. */
export const sizeBasis = (preview: ReviewPreview, t: Translate) =>
  preview.byte_size_basis === "images"
    ? t("解压后图片总大小", "Uncompressed image total")
    : preview.byte_size_basis === "resample"
      ? t("重采样版本，不比较大小", "Resampled copy; size not compared")
      : t("归档文件大小", "Archive size");

export function reviewIssue(
  issue: string,
  preview: ReviewPreview,
  t: Translate,
): string {
  const label = issueNames[issue] ? t(...issueNames[issue]) : issue;
  if (issue === "declared_byte_size_mismatch") {
    const declared = preview.metadata.declared_byte_size;
    const actual = preview.compared_byte_size;
    return `${label}: ${t("来源声明", "Declared")}: ${typeof declared === "number" ? exactBytes(declared) : t("未知", "Unknown")}; ${t("实际", "Actual")}: ${actual == null ? t("不可用", "Unavailable") : exactBytes(actual)} (${sizeBasis(preview, t)})`;
  }
  if (issue === "declared_page_count_mismatch")
    return `${label}: ${t("来源声明", "Declared")}: ${preview.metadata.declared_page_count ?? t("未知", "Unknown")}; ${t("实际", "Actual")}: ${preview.actual_page_count ?? t("未知", "Unknown")}`;
  return label;
}

export function ImportNotes({
  preview,
  t,
}: {
  preview: ReviewPreview;
  t: Translate;
}) {
  const warnings = preview.warnings;
  return (
    <>
      {preview.import_sources.length > 0 && (
        <p className="muted small">
          {t("信息来源：", "Metadata sources: ")}
          {preview.import_sources.map((source) => (
            <Pill key={source}>{importName(source, t)}</Pill>
          ))}
        </p>
      )}
      {warnings.length > 0 && (
        <div className="notice" role="status">
          {warnings.some((warning) => warning.startsWith("native_")) && (
            <p>
              {t(
                "Native 文件未能通过格式、内容绑定或稳定性检查，未采用其中的信息。书籍仍可阅读。",
                "The native file failed format, content-binding or stability checks. Its metadata was not used; the book remains readable.",
              )}
            </p>
          )}
          {warnings.some((warning) => warning.startsWith("eze_")) && (
            <p>
              {t(
                "Eze 文件未能读取，未采用其中的信息。",
                "The Eze file could not be read; its metadata was not used.",
              )}
            </p>
          )}
          {warnings.some(
            (warning) =>
              !warning.startsWith("native_") && !warning.startsWith("eze_"),
          ) && (
            <p>
              {t(
                "部分来源字段未通过检查，请核对已导入信息。",
                "Some source fields did not pass validation. Check the imported metadata.",
              )}
            </p>
          )}
        </div>
      )}
      {preview.ignored_fields.length > 0 && (
        <p className="muted small">
          {t(
            `已忽略 ${preview.ignored_fields.length} 个不支持的外部字段。`,
            `${preview.ignored_fields.length} unsupported external fields were ignored.`,
          )}
        </p>
      )}
    </>
  );
}
function Value({ value, t }: { value: unknown; t: Translate }) {
  if (
    Array.isArray(value) &&
    value.every(
      (item) =>
        item &&
        typeof item === "object" &&
        "value" in item &&
        "namespace" in item,
    )
  )
    return (
      <div className="tags">
        {value.map((tag, index) => (
          <span className="pill" key={index}>
            <TagText tag={tag} t={t} />
          </span>
        ))}
      </div>
    );
  return (
    <pre className="field-value">
      {value === null || value === undefined || value === ""
        ? t("未填写", "Not set")
        : typeof value === "string"
          ? value
          : JSON.stringify(value, null, 2)}
    </pre>
  );
}

export function Candidate({
  name,
  data,
  current,
  busy,
  t,
  submit,
}: {
  name: string;
  data: ReviewPreview["candidates"][string];
  current: Metadata;
  busy: boolean;
  t: Translate;
  submit: (name: string, choices: Record<string, string> | null) => void;
}) {
  const [choices, setChoices] = useState<Record<string, string>>(() =>
    Object.fromEntries(data.fields.map((field) => [field, "current"])),
  );
  if (!data.fields.length)
    return (
      <div className="candidate">
        <Pill>{importName(name, t)}</Pill>{" "}
        <Pill>{t("完全匹配", "Fully matches")}</Pill>
        {data.decision && <Pill>{t("已处理", "Handled")}</Pill>}
      </div>
    );
  return (
    <details className="candidate">
      <summary>
        <Pill>{importName(name, t)}</Pill>{" "}
        <Pill>
          {data.fields.length} {t("处差异", "differences")}
        </Pill>
        {data.decision && <Pill>{t("已处理", "Handled")}</Pill>}
      </summary>
      {data.fields.map((field) => (
        <fieldset className="field-choice" key={field}>
          <legend>{label(field, t)}</legend>
          {(["current", "candidate"] as const).map((source) => (
            <label key={source}>
              <span>
                <input
                  type="radio"
                  name={`${name}-${field}`}
                  checked={choices[field] === source}
                  onChange={() =>
                    setChoices((old) => ({ ...old, [field]: source }))
                  }
                />
                {source === "current"
                  ? t("保留当前", "Keep current")
                  : t("使用候选", "Use candidate")}
              </span>
              <Value
                value={
                  (source === "current" ? current : data.metadata)[
                    field as keyof Metadata
                  ]
                }
                t={t}
              />
            </label>
          ))}
        </fieldset>
      ))}
      <div className="actions">
        <button
          type="button"
          disabled={busy || data.fields.length === 0}
          onClick={() => submit(name, choices)}
        >
          {t("采用所选字段", "Apply selected fields")}
        </button>
        <button
          type="button"
          className="subtle"
          disabled={busy}
          onClick={() => submit(name, null)}
        >
          {t("跳过这份候选", "Skip this candidate")}
        </button>
      </div>
    </details>
  );
}

export function SidecarConflict({
  conflict,
  busy,
  t,
  submit,
}: {
  conflict: Conflict;
  busy: boolean;
  t: Translate;
  submit: (fields: Record<string, "library" | "file">) => void;
}) {
  const [choices, setChoices] = useState<Record<string, "library" | "file">>(
    () =>
      Object.fromEntries(conflict.fields.map((field) => [field, "library"])),
  );
  return (
    <section className="conflict-panel">
      <h3>
        {t(
          "随书信息文件与书库不一致",
          "Metadata file differs from the library",
        )}
      </h3>
      <p className="muted">
        {t(
          "确认前两边都保留，不会自动覆盖。",
          "Both versions are kept until you choose.",
        )}
      </p>
      {conflict.fields.map((field) => (
        <fieldset className="field-choice" key={field}>
          <legend>{label(field, t)}</legend>
          {(["library", "file"] as const).map((side) => (
            <label key={side}>
              <span>
                <input
                  type="radio"
                  name={`sidecar-${field}`}
                  checked={choices[field] === side}
                  onChange={() =>
                    setChoices((old) => ({ ...old, [field]: side }))
                  }
                />
                {side === "library"
                  ? t("保留书库", "Keep library")
                  : t("采用文件", "Take file")}
              </span>
              <Value
                value={conflict[side].metadata[field as keyof Metadata]}
                t={t}
              />
            </label>
          ))}
        </fieldset>
      ))}
      <button type="button" disabled={busy} onClick={() => submit(choices)}>
        {t("保存冲突选择", "Save conflict choices")}
      </button>
    </section>
  );
}

/** The saved-change notice for a sidecar sync result: plain "Saved." when the file is current. */
export function savedNotice(sync: SidecarSync | undefined, t: Translate) {
  if (sync?.state === "conflict")
    return t(
      "已保存；随书信息同步需要处理。",
      "Saved; metadata file sync needs attention.",
    );
  if (sync?.state === "pending")
    return t("已保存，文件同步待处理。", "Saved; file sync pending.");
  return t("已保存。", "Saved.");
}
