import { label } from "./ReviewDetails";
import { ReviewDialog } from "./ReviewDialog";
import { RemovalDialog } from "./RemovalDialog";
import { ActionMenu, Pagination, Select, ContextActions } from "./Controls";
import {
  BookPills,
  Pill,
  TagPill,
  languages,
  languageName,
  languageLabel,
} from "./Pills";
import { TagEditor } from "./TagEditor";
import { BackButton } from "./navigation";
import { dateTime } from "./format";
import { toast } from "./feedback";
import { SourceComments } from "./SourceDetail";
import { useEffect, useState, useMemo } from "react";
import { BookOpen } from "lucide-react";
import { ApiError, errorMessage, post, put, request } from "./api";
import { Modal } from "./Modal";
import { Rating } from "./Rating";
import { Cover } from "./Cover";
import { thumbnailPath } from "./pages";
import type { PersonalQueue, SaveStatus } from "./personal";
import { useBookTitle } from "./PreferenceContext";
import type { Book, Metadata, Tag, Translate } from "./types";

type MetadataForm = {
  title: string;
  original_title: string;
  language: string;
  tags: Tag[];
};
const formOf = (metadata: Metadata): MetadataForm => ({
  title: metadata.title || "",
  original_title: metadata.original_title || "",
  language: metadata.language || "",
  tags: metadata.tags,
});
const sameTags = (a: Tag[], b: Tag[]) =>
  a.length === b.length &&
  a.every(
    (tag, index) =>
      tag.namespace === b[index]!.namespace && tag.value === b[index]!.value,
  );
const sameForm = (a: MetadataForm, b: MetadataForm) =>
  a.title === b.title &&
  a.original_title === b.original_title &&
  a.language === b.language &&
  sameTags(a.tags, b.tags);

export function Detail({
  navigate,
  search,
  suffix = "",
  book,
  update,
  queue,
  status,
  t,
  read,
  dirty,
}: {
  navigate: (route: string) => void;
  /** Runs a library search; tag pills use it. */
  search?: (query: string) => void;
  /** Route suffix of the current context (e.g. `/in/<collection>`), kept by page links. */
  suffix?: string;
  dirty: { current: boolean };
  book: Book;
  update: (book: Book) => void;
  queue: PersonalQueue;
  status: SaveStatus;
  t: Translate;
  read: () => void;
}) {
  const title = useBookTitle();
  const [removing, setRemoving] = useState(false);
  const removalBooks = useMemo(() => [book], [book]);
  const secondaryTitle = [
    book.metadata.title,
    book.metadata.original_title,
  ].find((value) => value && value !== title(book));
  const [form, setForm] = useState(() => formOf(book.metadata));
  const [busy, setBusy] = useState(false);
  /** Failure of a metadata save; shown inside the metadata dialog. */
  const [error, setError] = useState<unknown>(null);
  /** A newer server version was applied after a 409; the draft is kept. */
  const [conflicted, setConflicted] = useState(false);
  const [confirmDiscard, setConfirmDiscard] = useState(false);
  const [{ page: thumbnailPage, size: thumbnailSize }, setThumbnails] =
    useState(() => ({
      page: 1,
      size: window.matchMedia("(width < 700px)").matches ? 8 : 20,
    }));
  useEffect(() => {
    const compact = window.matchMedia("(width < 700px)");
    const resize = () =>
      setThumbnails(({ page, size }) => {
        const nextSize = compact.matches ? 8 : 20;
        return {
          page: Math.floor(((page - 1) * size) / nextSize) + 1,
          size: nextSize,
        };
      });
    compact.addEventListener("change", resize);
    return () => compact.removeEventListener("change", resize);
  }, []);
  const searchTag = (namespace: string, value: string) =>
    search?.(`${namespace || "other"}:${JSON.stringify(value)}`);
  const [dialog, setDialog] = useState<"metadata" | "review" | null>(null);
  const editing = dialog === "metadata";
  const saved = formOf(book.metadata);
  const changed = !sameForm(form, saved);
  /** Closing the editor never silently drops a draft: a dirty form asks first. */
  const closeMetadata = (discard = false) => {
    if (changed && !discard) {
      setConfirmDiscard(true);
      return;
    }
    setConfirmDiscard(false);
    setConflicted(false);
    setError(null);
    setForm(formOf(book.metadata));
    setDialog(null);
  };
  // While the editor is closed the form mirrors the book; while it is open, newer
  // server versions (e.g. after a conflict) leave the draft alone.
  useEffect(() => {
    if (!editing) setForm(formOf(book.metadata));
  }, [book.metadata, editing]);
  async function saveMetadata() {
    setBusy(true);
    setError(null);
    const metadata: Metadata = {
      ...book.metadata,
      title: form.title || null,
      original_title: form.original_title || null,
      language: form.language || null,
      tags: form.tags,
    };
    try {
      const stored = await put<Book>(`/api/books/${book.id}/metadata`, {
        expected_revision: book.metadata_revision,
        metadata,
      });
      update(stored);
      // Show what the server stored (it may normalize values), so the form is clean.
      setForm(formOf(stored.metadata));
    } catch (error) {
      setError(error);
      if (error instanceof ApiError && error.status === 409) {
        // Apply the newer version now (the draft stays) so the next save can succeed.
        await request<Book>(`/api/books/${book.id}`).then(
          (newer) => {
            update(newer);
            setConflicted(true);
          },
          () => {},
        );
      }
      setBusy(false);
      return;
    }
    setConflicted(false);
    // The save is committed; a failing file sync or refresh does not undo it.
    let saved = t("已保存。", "Saved.");
    try {
      const result = await post<{ state: string }>("/api/sidecar/sync", {
        book_id: book.id,
      });
      if (result.state === "conflict" || result.state === "error")
        saved = t(
          "已保存；随书信息同步需要处理。",
          "Saved; metadata file sync needs attention.",
        );
      update(await request<Book>(`/api/books/${book.id}`));
    } catch {
      saved = t(
        "已保存；随书信息文件同步失败。",
        "Saved; metadata file sync failed.",
      );
    }
    toast.success(saved, { id: "metadata-saved" });
    setBusy(false);
  }
  useEffect(() => {
    dirty.current = changed;
    return () => {
      dirty.current = false;
    };
  }, [changed, dirty]);
  return (
    <ContextActions
      actions={[
        {
          label: t("开始 / 继续阅读", "Start / continue reading"),
          action: read,
        },
        {
          label: t("编辑元数据", "Edit metadata"),
          action: () => setDialog("metadata"),
        },
        {
          label: t("移除", "Remove"),
          danger: true,
          disabled: busy || status !== "saved",
          action: () => setRemoving(true),
        },
      ]}
    >
      <main className="detail content">
        <BackButton t={t} />
        <section className="book-overview">
          <div className="detail-cover-group">
            <div className="detail-cover">
              <Cover
                id={book.id}
                sha256={book.content.sha256}
                alt={t("封面", "Cover")}
              />
            </div>
            <Pill kind="reading">
              {book.personal.completed
                ? t("已读完", "Finished")
                : book.personal.resume_page === null
                  ? t("未读", "Unread")
                  : t("在读", "Reading")}
            </Pill>
          </div>
          <div className="book-intro">
            <div className="tags">
              {book.review_state !== "confirmed" && (
                <button
                  type="button"
                  className="pill needs-review"
                  onClick={() => setDialog("review")}
                >
                  {t("需核对", "Needs review")}
                </button>
              )}
              <BookPills book={book} t={t} file rating={false} />
            </div>
            <h1>{title(book)}</h1>
            {secondaryTitle && (
              <p className="muted original-title">{secondaryTitle}</p>
            )}
            <div className="actions">
              <button type="button" className="primary" onClick={read}>
                <BookOpen size={18} />
                {book.personal.resume_page === null
                  ? t("开始阅读", "Start reading")
                  : t("继续阅读", "Continue reading")}
              </button>
              <ActionMenu
                label={t("书籍操作", "Book actions")}
                actions={[
                  ...(book.review_state !== "confirmed"
                    ? [
                        {
                          label: t("核对信息", "Review metadata"),
                          action: () => setDialog("review"),
                        },
                      ]
                    : []),
                  {
                    label: t("编辑元数据", "Edit metadata"),
                    action: () => setDialog("metadata"),
                  },
                  {
                    label: t("从来源更新", "Update from source"),
                    action: () => setDialog("review"),
                  },
                  {
                    label: t("移除", "Remove"),
                    action: () => setRemoving(true),
                    danger: true,
                    disabled: busy || status !== "saved",
                  },
                ]}
              />
            </div>
            <div className="intro-rating">
              <Rating
                value={book.personal.rating}
                t={t}
                change={(value) =>
                  queue.update((state) => ({ ...state, rating: value }))
                }
              />
              {book.metadata.source_rating && (
                <Pill>
                  {t("来源评分", "Source rating")}{" "}
                  {book.metadata.source_rating.average} ★
                </Pill>
              )}
            </div>
          </div>
        </section>
        {status === "error" && (
          <div role="alert" className="notice error">
            {t(
              "个人修改未保存。重试会在最新状态上应用你的操作。",
              "Personal changes are not saved. Retry reapplies your actions to the latest state.",
            )}{" "}
            <button type="button" onClick={() => void queue.retry()}>
              {t("重试保存", "Retry saving")}
            </button>
            <button type="button" onClick={() => void queue.reload()}>
              {t(
                "丢弃未保存修改并重新载入",
                "Discard unsaved changes and reload",
              )}
            </button>
          </div>
        )}
        <section className="panel book-information">
          <h2>{t("标签", "Tags")}</h2>
          <dl className="book-facts">
            {Object.entries(
              book.metadata.tags
                .filter(
                  (tag) =>
                    !(
                      tag.namespace === "language" &&
                      !["Other", "Unknown"].includes(
                        languageName(book.metadata.language),
                      )
                    ) &&
                    !(
                      ["category", "reclass"].includes(tag.namespace) &&
                      book.metadata.work_type
                    ),
                )
                .reduce<Record<string, typeof book.metadata.tags>>(
                  (groups, tag) => {
                    (groups[tag.namespace || "other"] ??= []).push(tag);
                    return groups;
                  },
                  {},
                ),
            ).map(([namespace, tags]) => (
              <div className="tag-row" key={namespace}>
                <dt>
                  {namespace === "other" ? t("其他", "Other") : namespace}
                </dt>
                <dd>
                  {tags.map((tag) => (
                    <TagPill
                      key={tag.value}
                      tag={tag}
                      translation={
                        book.tag_translations?.find(
                          (label) =>
                            label.namespace === tag.namespace &&
                            label.value === tag.value,
                        )?.translation
                      }
                      count={
                        book.tag_counts?.find(
                          (item) =>
                            (item.namespace || "other") ===
                              (tag.namespace || "other") &&
                            item.value === tag.value,
                        )?.count
                      }
                      click={() => searchTag(tag.namespace, tag.value)}
                    />
                  ))}
                </dd>
              </div>
            ))}
          </dl>
        </section>
        <section className="panel page-thumbnails">
          <h2>{t("页面", "Pages")}</h2>
          <div className="thumbnail-grid">
            {Array.from(
              {
                length: Math.min(
                  thumbnailSize,
                  book.content.page_count - (thumbnailPage - 1) * thumbnailSize,
                ),
              },
              (_, index) => (thumbnailPage - 1) * thumbnailSize + index,
            ).map((index) => (
              <a
                key={index}
                href={`#read/${book.id}${suffix}/page/${index}`}
                aria-label={t(
                  `阅读第 ${index + 1} 页`,
                  `Read page ${index + 1}`,
                )}
              >
                <img
                  src={thumbnailPath(book.id, index, book.content.sha256)}
                  loading="lazy"
                  decoding="async"
                  alt=""
                />
                <Pill>{index + 1}</Pill>
              </a>
            ))}
          </div>
          <Pagination
            page={thumbnailPage}
            total={book.content.page_count}
            size={thumbnailSize}
            change={(page) =>
              setThumbnails((current) => ({ ...current, page }))
            }
            t={t}
          />
        </section>
        <SourceComments book={book} t={t} />
        <section className="panel">
          <dl className="book-facts">
            <dt>{t("文件名", "Filename")}</dt>
            <dd>{book.content.original_filename}</dd>
            <dt>{t("上传者", "Uploader")}</dt>
            <dd>{book.metadata.uploader || t("未知", "Unknown")}</dd>
            <dt>{t("上传时间", "Uploaded")}</dt>
            <dd>
              {book.metadata.uploaded_at
                ? dateTime(book.metadata.uploaded_at, t("zh-CN", "en-US"))
                : t("未知", "Unknown")}
            </dd>
          </dl>
        </section>
        {dialog === "metadata" && (
          <Modal
            title={t("编辑元数据", "Edit metadata")}
            closeLabel={t("关闭", "Close")}
            busy={busy}
            close={() => closeMetadata()}
          >
            {!!error && (
              <div role="alert" className="notice error">
                {errorMessage(error, t)}
                {conflicted && (
                  <>
                    {" "}
                    {t(
                      "已载入最新版本，你的修改仍保留；保存将覆盖它，或改为载入最新内容。",
                      "The latest version is loaded and your edits are kept; saving replaces it, or reload the latest values.",
                    )}{" "}
                    <button
                      type="button"
                      disabled={busy}
                      onClick={() => {
                        setForm(formOf(book.metadata));
                        setError(null);
                        setConflicted(false);
                      }}
                    >
                      {t("载入最新内容", "Reload latest")}
                    </button>
                  </>
                )}
              </div>
            )}
            {confirmDiscard && (
              <div role="alert" className="notice">
                {t("放弃未保存的修改？", "Discard unsaved changes?")}{" "}
                <button
                  type="button"
                  className="danger"
                  onClick={() => closeMetadata(true)}
                >
                  {t("放弃修改", "Discard changes")}
                </button>{" "}
                <button type="button" onClick={() => setConfirmDiscard(false)}>
                  {t("继续编辑", "Keep editing")}
                </button>
              </div>
            )}
            <form
              onSubmit={(event) => {
                event.preventDefault();
                if (!busy) void saveMetadata();
              }}
              className="metadata-form"
            >
              {(["title", "original_title"] as const).map((field) => (
                <label key={field}>
                  {label(field, t)}
                  <input
                    value={form[field]}
                    maxLength={4096}
                    onChange={(event) =>
                      setForm((old) => ({
                        ...old,
                        [field]: event.target.value,
                      }))
                    }
                  />
                </label>
              ))}
              <label>
                {t("语言", "Language")}
                <Select
                  value={languageName(form.language)}
                  onChange={(event) =>
                    setForm((old) => ({
                      ...old,
                      language:
                        event.target.value === "Unknown"
                          ? ""
                          : event.target.value.toLowerCase(),
                    }))
                  }
                >
                  {languages.map((language) => (
                    <option key={language} value={language}>
                      {languageLabel(language, t)}
                    </option>
                  ))}
                </Select>
              </label>
              <label>{t("标签", "Tags")}</label>
              <TagEditor
                value={form.tags}
                change={(tags) => setForm((old) => ({ ...old, tags }))}
                t={t}
              />
              <div className="actions">
                <button type="submit" disabled={busy || !changed}>
                  {t("保存信息", "Save metadata")}
                </button>
              </div>
            </form>
          </Modal>
        )}
        {dialog === "review" && (
          <ReviewDialog
            book={book}
            update={update}
            t={t}
            changed={changed}
            close={() => setDialog(null)}
          />
        )}
        {removing && (
          <RemovalDialog
            books={removalBooks}
            t={t}
            close={() => setRemoving(false)}
            removed={() => {
              setRemoving(false);
              navigate("");
            }}
          />
        )}
      </main>
    </ContextActions>
  );
}
