import { tagQuery } from "./Pills";
import { label, savedNotice } from "./ReviewDetails";
import { ReviewDialog } from "./ReviewDialog";
import { RemovalDialog } from "./RemovalDialog";
import { EDIT_INTENT, useBookActions } from "./bookActions";
import { ActionMenu, Pagination, Select } from "./Controls";
import {
  BookPills,
  Pill,
  TagPill,
  languages,
  languageName,
  languageLabel,
  NamespaceName,
} from "./Pills";
import { TagEditor, withTags, type TagDraft } from "./TagEditor";
import { BackButton, RouteLink } from "./navigation";
import { dateTime } from "./format";
import { toast } from "./feedback";
import { SourceComments, useSourceDiscussion } from "./SourceDetail";
import { SourceCheckStatus } from "./SourceCheckStatus";
import { SaveFailure } from "./SaveFailure";
import { useEffect, useId, useState, useMemo, useRef } from "react";
import { BookOpen, ExternalLink, FileWarning } from "lucide-react";
import { ApiError, errorMessage, put, request } from "./api";
import { Modal } from "./Modal";
import { Rating } from "./Rating";
import { Cover } from "./Cover";
import { thumbnailPath } from "./pages";
import { SITE_LABEL, sourceURL } from "./identifiers";
import type { PersonalQueue, SaveStatus } from "./personal";
import { readingStatus } from "./reading";
import { useBookTitle } from "./PreferenceContext";
import type { Book, Metadata, SidecarSync, Tag, Translate } from "./types";

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

/** Why an unavailable book's pages cannot open: a short label and what to do about it. */
export function unavailable(availability: Book["availability"], t: Translate) {
  switch (availability) {
    case "missing":
      return {
        label: t("文件缺失", "File missing"),
        detail: t(
          "找不到这本书的文件，页面无法打开。请把文件放回原位置，或重新扫描书库。",
          "This book's file is missing, so its pages cannot open. Put the file back or scan the library again.",
        ),
      };
    case "changed":
      return {
        label: t("文件已被修改", "File changed"),
        detail: t(
          "这本书的文件已被修改，页面可能无法打开。重新导入原文件即可修复，改动过的文件会以 .changed 结尾保留。",
          "This book's file changed on disk, so its pages may not open. Import the original file again to repair it; the modified file is kept with a .changed suffix.",
        ),
      };
    case "unreadable":
      return {
        label: t("文件无法读取", "File unreadable"),
        detail: t(
          "这本书的文件无法读取，页面无法打开。请检查文件和存储权限。",
          "This book's file cannot be read, so its pages cannot open. Check the file and storage permissions.",
        ),
      };
    default:
      return {
        label: t("文件暂不可用", "File unavailable"),
        detail: t(
          "这本书的文件暂不可用，页面无法打开。请检查文件。",
          "This book's file is unavailable, so its pages cannot open. Check the file.",
        ),
      };
  }
}

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
  const tagsLabel = useId();
  const discussion = useSourceDiscussion(book);
  const bookActions = useBookActions({
    t,
    navigate,
    // Refreshed thumbnails come with a new revision in the book.
    changed: () =>
      void request<Book>(`/api/books/${book.id}`).then(update, () => {}),
  });
  const [removing, setRemoving] = useState(false);
  const removalBooks = useMemo(() => [book], [book]);
  const secondaryTitle = [
    book.metadata.title,
    book.metadata.original_title,
  ].find((value) => value && value !== title(book));
  /** The latest version the source lists, when the book is not it. */
  const newest = book.metadata.newer_versions.at(-1) ?? null;
  const [form, setForm] = useState(() => formOf(book.metadata));
  const formBase = useRef(formOf(book.metadata));
  /** A tag typed but not yet added with Enter; saving includes it. */
  const [tagDraft, setTagDraft] = useState<TagDraft | null>(null);
  /** Remounting the tag editor clears its entry once a save has taken it. */
  const [tagEditor, setTagEditor] = useState(0);
  /**
   * Counts edits to the form, so a save that returns after further typing keeps that
   * typing instead of replacing the form with what the server stored.
   */
  const edits = useRef(0);
  const edit = (change: (old: MetadataForm) => MetadataForm) => {
    edits.current++;
    setForm(change);
  };
  const typedTag = useRef("null");
  /** Shows `metadata` in the form, as it is stored; not an edit. */
  const resetForm = (metadata: Metadata) => {
    formBase.current = formOf(metadata);
    setForm(formBase.current);
    typedTag.current = "null";
    setTagDraft(null);
    setTagEditor((value) => value + 1);
  };
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
    search?.(tagQuery(namespace, value));
  // "Edit metadata" in a library menu opens this page with its editor.
  const [dialog, setDialog] = useState<"metadata" | "review" | null>(() => {
    try {
      const intent = sessionStorage.getItem(EDIT_INTENT);
      if (intent !== book.id && intent !== `review:${book.id}`) return null;
      sessionStorage.removeItem(EDIT_INTENT);
      return intent === book.id ? "metadata" : "review";
    } catch {
      return null;
    }
  });
  const editing = dialog === "metadata";
  const saved = formOf(book.metadata);
  const draft = tagDraft
    ? { ...form, tags: withTags(form.tags, tagDraft.tags, tagDraft.replace) }
    : form;
  const changed = !sameForm(draft, saved);
  /** Closing the editor never silently drops a draft: a dirty form asks first. */
  const closeMetadata = (discard = false) => {
    if (changed && !discard) {
      setConfirmDiscard(true);
      return;
    }
    setConfirmDiscard(false);
    setConflicted(false);
    setError(null);
    resetForm(book.metadata);
    setDialog(null);
  };
  // While the editor is closed the form mirrors the book; while it is open, newer
  // server versions (e.g. after a conflict) leave the draft alone.
  useEffect(() => {
    if (!editing) {
      formBase.current = formOf(book.metadata);
      setForm(formBase.current);
    }
  }, [book.metadata, editing]);
  async function saveMetadata() {
    setBusy(true);
    setError(null);
    const version = edits.current;
    let sync: SidecarSync | undefined;
    const metadata: Metadata = {
      ...book.metadata,
      title: draft.title || null,
      original_title: draft.original_title || null,
      language: draft.language || null,
      tags: draft.tags,
    };
    try {
      const stored = await put<Book & { sidecar_sync?: SidecarSync }>(
        `/api/books/${book.id}/metadata?expected_revision=${book.metadata_revision}`,
        { metadata },
      );
      update(stored);
      sync = stored.sidecar_sync;
      // Show what the server stored (it may normalize values), so the form is clean;
      // input typed while the request was out stays and still counts as a change.
      if (edits.current === version) resetForm(stored.metadata);
    } catch (error) {
      setError(error);
      if (error instanceof ApiError && error.status === 409) {
        // Apply the newer version now (the draft stays) so the next save can succeed.
        await request<Book>(`/api/books/${book.id}`).then(
          (newer) => {
            const fresh = formOf(newer.metadata);
            const base = formBase.current;
            setForm((current) => ({
              title: current.title === base.title ? fresh.title : current.title,
              original_title:
                current.original_title === base.original_title
                  ? fresh.original_title
                  : current.original_title,
              language:
                current.language === base.language
                  ? fresh.language
                  : current.language,
              tags:
                JSON.stringify(current.tags) === JSON.stringify(base.tags)
                  ? fresh.tags
                  : current.tags,
            }));
            formBase.current = fresh;
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
    // The save is committed; a file the server writes later is not a failure.
    let saved = savedNotice(sync, t);
    if (edits.current !== version)
      saved += t(
        " 保存期间的新修改尚未保存。",
        " Changes made while saving are not saved yet.",
      );
    toast.success(saved, { id: "metadata-saved" });
    setBusy(false);
    // A clean save closes the editor; input typed while it was out keeps it open.
    if (edits.current === version) {
      setConfirmDiscard(false);
      setDialog(null);
    }
  }
  useEffect(() => {
    dirty.current = changed;
    return () => {
      dirty.current = false;
    };
  }, [changed, dirty]);
  const targets = {
    bookIds: [book.id],
    anchor: book,
    unfinished: !book.personal.completed,
  };
  const menu = bookActions.menu(targets, {
    read,
    edit: () => setDialog("metadata"),
    review: () => setDialog("review"),
    remove: () => setRemoving(true),
    disabled: busy || status !== "saved",
    completed: (completed) =>
      queue.update((state) => ({
        ...state,
        completed,
        ...(completed ? {} : { resume_page: null }),
      })),
    rating: (rating) => queue.update((state) => ({ ...state, rating })),
  });
  // The page keeps the browser's own context menu (copy text, open an image); its
  // actions are on the visible buttons.
  return (
    <>
      <main className="detail content">
        <BackButton t={t} />
        <section className="book-overview">
          <div className="detail-cover-group">
            <div className="detail-cover">
              <Cover
                id={book.id}
                sha256={book.content.sha256}
                revision={book.thumbnail_revision}
                alt={t("封面", "Cover")}
              />
            </div>
            <div className="detail-badges">
              <Pill kind="reading">
                {readingStatus(book.personal) === "finished"
                  ? t("已读完", "Finished")
                  : readingStatus(book.personal) === "unread"
                    ? t("未读", "Unread")
                    : t("阅读中", "Reading")}
              </Pill>
              {book.metadata.source && (
                <a
                  className="pill pill-source"
                  href={sourceURL(book.metadata.source)}
                  target="_blank"
                  rel="noreferrer"
                  title={t("在来源网站打开", "Open at the source site")}
                >
                  <ExternalLink size={12} aria-hidden="true" />
                  {SITE_LABEL}
                </a>
              )}
              {newest && (
                <a
                  className="pill pill-newer"
                  href={sourceURL(newest)}
                  target="_blank"
                  rel="noreferrer"
                  title={t(
                    "来源上有更新的版本",
                    "The source lists a newer version",
                  )}
                >
                  <ExternalLink size={12} aria-hidden="true" />
                  {t("有新版本", "Newer version")}
                </a>
              )}
            </div>
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
              <RouteLink
                className="primary"
                route={`read/${book.id}${suffix}`}
                navigate={read}
              >
                <BookOpen size={18} />
                {readingStatus(book.personal) === "finished"
                  ? t("重新阅读", "Read again")
                  : readingStatus(book.personal) === "unread"
                    ? t("开始阅读", "Start reading")
                    : t("继续阅读", "Continue reading")}
              </RouteLink>
              <ActionMenu
                label={t("书籍操作", "Book actions")}
                actions={menu}
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
        {book.availability !== "available" && (
          <p role="status" className="notice error availability">
            <FileWarning size={18} aria-hidden="true" />
            {unavailable(book.availability, t).detail}
          </p>
        )}
        {status === "error" && (
          <SaveFailure
            className="notice error"
            queue={queue}
            t={t}
            lead={t(
              "个人修改未保存。重试会在最新状态上应用你的操作。",
              "Personal changes are not saved. Retry reapplies your actions to the latest state.",
            )}
          />
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
                  <NamespaceName namespace={namespace} t={t} />
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
                href={`#read/${book.id}${suffix}/page/${index + 1}`}
                aria-label={t(
                  `阅读第 ${index + 1} 页`,
                  `Read page ${index + 1}`,
                )}
              >
                <img
                  src={thumbnailPath(
                    book.id,
                    index,
                    book.content.sha256,
                    book.thumbnail_revision,
                  )}
                  className={
                    (book.page_sizes?.[index]?.[0] ?? 0) >
                    (book.page_sizes?.[index]?.[1] ?? Infinity)
                      ? "wide-thumbnail"
                      : undefined
                  }
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
        <SourceCheckStatus book={book} update={update} t={t} />
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
        <SourceComments book={book} discussion={discussion} t={t} />
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
                      "已合并其他字段的最新值，你修改的字段仍保留；再次保存将应用这些修改，或载入最新内容放弃草稿。",
                      "New values for untouched fields are merged. Your edited fields are kept; save again to apply them, or reload to discard the draft.",
                    )}{" "}
                    <button
                      type="button"
                      disabled={busy}
                      onClick={() => {
                        resetForm(book.metadata);
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
                <button type="button" onClick={() => setConfirmDiscard(false)}>
                  {t("继续编辑", "Keep editing")}
                </button>{" "}
                <button
                  type="button"
                  className="danger"
                  onClick={() => closeMetadata(true)}
                >
                  {t("放弃修改", "Discard changes")}
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
                      edit((old) => ({
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
                    edit((old) => ({
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
              <div
                className="field-group"
                role="group"
                aria-labelledby={tagsLabel}
              >
                <span id={tagsLabel}>{t("标签", "Tags")}</span>
                <TagEditor
                  key={tagEditor}
                  value={form.tags}
                  change={(tags) => edit((old) => ({ ...old, tags }))}
                  pending={(value) => {
                    const key = JSON.stringify(value);
                    if (key !== typedTag.current) edits.current++;
                    typedTag.current = key;
                    setTagDraft(value);
                  }}
                  t={t}
                />
              </div>
              <div className="actions modal-actions">
                <button
                  type="button"
                  disabled={busy}
                  onClick={() => closeMetadata()}
                >
                  {t("取消", "Cancel")}
                </button>
                <button
                  type="submit"
                  className="primary"
                  disabled={busy || !changed}
                >
                  {t("保存信息", "Save metadata")}
                </button>
              </div>
            </form>
          </Modal>
        )}
        {dialog === "review" && (
          <ReviewDialog
            book={book}
            notes={discussion.data?.notes ?? []}
            update={update}
            t={t}
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
              navigate(suffix ? `collection/${suffix.split("/")[2]}` : "");
            }}
          />
        )}
        {bookActions.dialogs}
      </main>
    </>
  );
}
