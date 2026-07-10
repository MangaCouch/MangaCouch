// Archive detail page: cover, title, author/circle, grouped namespaced tags
// (localized, owner-editable), language, star rating (PUTs metadata), page
// count, engagement counts, preview thumbnail grid, comments, a simple
// favorite toggle, similar / same-series sections, a Read button and source
// links.

import { useCallback, useMemo, useState } from 'react';
import { Link, useNavigate, useParams } from 'react-router-dom';
import {
  archiveDownloadUrl,
  deleteArchive,
  getArchive,
  listArchives,
  listPlugins,
  runMetadataPlugin,
  setFavorite,
  setRating,
  thumbnailUrl,
  updateMetadata,
} from '../api/endpoints';
import type { Archive, PluginInfo, Tag } from '../api/types';
import { useAsync } from '../hooks/useApi';
import { useAuth } from '../hooks/useAuth';
import { SmartImage } from '../components/SmartImage';
import { Spinner, ErrorBanner, StarRating, Modal } from '../components/ui';
import { toast } from '../components/Toast';
import {
  authorTags,
  groupByNamespace,
  isRead,
  languageTags,
  progressFraction,
  progressPage,
  tagDisplay,
  tagToken,
} from '../lib/tags';
import { t } from '../i18n/strings';

export function Detail() {
  const { id = '' } = useParams();
  const navigate = useNavigate();
  const { isOwner } = useAuth();

  const { data, error, loading, reload } = useAsync<Archive>(
    (signal) => getArchive(id, signal),
    [id],
  );

  if (loading) return <Spinner label={t('library.loading')} />;
  if (error) return <ErrorBanner error={error} onRetry={reload} />;
  if (!data) return null;

  return (
    <DetailContent
      archive={data}
      isOwner={isOwner}
      onChanged={reload}
      onDeleted={() => navigate('/')}
    />
  );
}

/** A tag value that is a URL (e.g. source:nhentai.net/g/123) renders as an external link. */
function tagHref(tag: Tag): string | null {
  const v = tag.value.trim();
  if (/^https?:\/\//i.test(v)) return v;
  // Bare domain-ish values in the source namespace (eze/nhentai convention).
  if (tag.namespace === 'source' && /^[\w.-]+\.[a-z]{2,}\//i.test(v)) return `https://${v}`;
  return null;
}

function DetailContent({
  archive,
  isOwner,
  onChanged,
  onDeleted,
}: {
  archive: Archive;
  isOwner: boolean;
  onChanged: () => void;
  onDeleted: () => void;
}) {
  const [rating, setRatingLocal] = useState<number>(archive.rating ?? 0);
  const [savingRating, setSavingRating] = useState(false);
  const read = isRead(archive);
  const frac = progressFraction(archive);
  const continuePage = !read ? progressPage(archive) : 0;

  const authors = authorTags(archive.tags);
  const languages = languageTags(archive.tags);
  const grouped = useMemo(() => groupByNamespace(archive.tags), [archive.tags]);

  const onRate = useCallback(
    async (value: number) => {
      if (!isOwner) return;
      setRatingLocal(value);
      setSavingRating(true);
      try {
        await setRating(archive.id, value);
        toast.success(t('detail.rating.saved'));
        onChanged();
      } catch (err) {
        // Roll back the optimistic value so the UI reflects the server state.
        setRatingLocal(archive.rating ?? 0);
        toast.error(err instanceof Error ? err.message : String(err));
      } finally {
        setSavingRating(false);
      }
    },
    [archive.id, archive.rating, isOwner, onChanged],
  );

  const onDelete = useCallback(async () => {
    if (!window.confirm(t('detail.confirmDelete'))) return;
    try {
      await deleteArchive(archive.id);
      toast.success(t('detail.deleted'));
      onDeleted();
    } catch (err) {
      toast.error(err instanceof Error ? err.message : String(err));
    }
  }, [archive.id, onDeleted]);

  return (
    <div className="detail">
      <div className="detail__top">
        <div className="detail__cover">
          <SmartImage
            src={thumbnailUrl(archive.id)}
            alt={archive.title}
            className="detail__cover-img"
            loading="eager"
          />
        </div>

        <div className="detail__info">
          <h1 className="detail__title">{archive.title}</h1>

          {authors.length > 0 && (
            <div className="detail__authors">
              {authors.map((a) => (
                <span key={tagToken(a)} className="detail__author">
                  {tagDisplay(a)}
                </span>
              ))}
            </div>
          )}

          <div className="detail__facts">
            <div className="detail__fact">
              <span className="detail__fact-label">{t('detail.pages')}</span>
              <span>{archive.page_count}</span>
            </div>
            {languages.length > 0 && (
              <div className="detail__fact">
                <span className="detail__fact-label">{t('detail.language')}</span>
                <span>{languages.map(tagDisplay).join(', ')}</span>
              </div>
            )}
            {(archive.love_count != null || archive.view_count != null) && (
              <div className="detail__fact">
                <span className="detail__fact-label">♥ / ⊙</span>
                <span>
                  {archive.love_count ?? 0} / {archive.view_count ?? 0}
                </span>
              </div>
            )}
          </div>

          <div className="detail__rating">
            <span className="detail__fact-label">{t('detail.rating')}</span>
            <StarRating value={rating} onChange={onRate} readOnly={!isOwner} />
            {archive.rating != null && (
              <span className="detail__rating-num">{archive.rating.toFixed(2)}</span>
            )}
            {savingRating && <span className="detail__saving">…</span>}
          </div>

          {frac > 0 && (
            <div className="detail__progress">
              <div className="detail__progress-bar" style={{ width: `${frac * 100}%` }} />
            </div>
          )}

          <div className="detail__actions">
            <Link
              to={`/read/${archive.id}${continuePage > 0 ? `?page=${continuePage}` : ''}`}
              className="btn btn--primary"
            >
              {continuePage > 0 ? t('detail.continue') : t('detail.read')}
            </Link>
            <FavoriteToggle archive={archive} onChanged={onChanged} />
            {archive.source_url && (
              <a
                href={archive.source_url}
                target="_blank"
                rel="noreferrer noopener"
                className="btn"
              >
                ↗ {t('detail.download')}
              </a>
            )}
            <a href={archiveDownloadUrl(archive.id)} className="btn" download>
              ⭳ {t('detail.downloadFile')}
            </a>
            {isOwner && (
              <FetchMetadataButton archive={archive} onChanged={onChanged} />
            )}
            {isOwner && (
              <button type="button" className="btn btn--danger" onClick={onDelete}>
                {t('detail.delete')}
              </button>
            )}
          </div>
        </div>
      </div>

      {archive.summary && <p className="detail__summary">{archive.summary}</p>}

      <section className="detail__section">
        <div className="detail__section-head">
          <h2>{t('detail.tags')}</h2>
          {isOwner && <EditTagsButton archive={archive} onChanged={onChanged} />}
        </div>
        {grouped.length > 0 && (
          <div className="detail__tags">
            {grouped.map(([ns, tags]) => (
              <div key={ns} className="detail__tag-group">
                <span className="detail__tag-ns">{ns}</span>
                <div className="detail__tag-chips">
                  {tags.map((tag) => {
                    const href = tagHref(tag);
                    if (href) {
                      return (
                        <a
                          key={tagToken(tag)}
                          href={href}
                          target="_blank"
                          rel="noreferrer noopener"
                          className="chip chip--clickable chip--link"
                          title={href}
                        >
                          {tag.value} ↗
                        </a>
                      );
                    }
                    return (
                      <Link
                        key={tagToken(tag)}
                        to={`/?q=${encodeURIComponent(tagToken(tag))}`}
                        className="chip chip--clickable"
                        title={tagToken(tag)}
                      >
                        {tagDisplay(tag)}
                      </Link>
                    );
                  })}
                </div>
              </div>
            ))}
          </div>
        )}
      </section>

      <PreviewGrid archive={archive} />

      <CommentsSection archive={archive} />

      <RelatedSections archive={archive} />
    </div>
  );
}

/** Simple favorite / unfavorite toggle (no list management). */
function FavoriteToggle({
  archive,
  onChanged,
}: {
  archive: Archive;
  onChanged: () => void;
}) {
  const [fav, setFav] = useState<boolean>(archive.favorite ?? false);
  const [busy, setBusy] = useState(false);

  const toggle = useCallback(async () => {
    if (busy) return;
    const next = !fav;
    setFav(next); // optimistic
    setBusy(true);
    try {
      await setFavorite(archive.id, next);
      onChanged();
    } catch (err) {
      setFav(!next);
      toast.error(err instanceof Error ? err.message : String(err));
    } finally {
      setBusy(false);
    }
  }, [archive.id, busy, fav, onChanged]);

  return (
    <button
      type="button"
      className={`btn ${fav ? 'btn--fav-active' : ''}`}
      onClick={toggle}
      disabled={busy}
      aria-pressed={fav}
    >
      {fav ? '♥' : '♡'} {fav ? t('detail.favorited') : t('detail.favorite')}
    </button>
  );
}

/** Owner action: edit all tags in one textarea (`namespace:value` per line). */
function EditTagsButton({
  archive,
  onChanged,
}: {
  archive: Archive;
  onChanged: () => void;
}) {
  const [open, setOpen] = useState(false);
  const [text, setText] = useState('');
  const [busy, setBusy] = useState(false);

  const openEditor = useCallback(() => {
    setText(archive.tags.map((tag) => tagToken(tag)).join('\n'));
    setOpen(true);
  }, [archive.tags]);

  const save = useCallback(async () => {
    const tags = text
      .split(/[\n,]/)
      .map((s) => s.trim())
      .filter(Boolean);
    setBusy(true);
    try {
      await updateMetadata(archive.id, { tags });
      toast.success(t('detail.editTags.saved'));
      setOpen(false);
      onChanged();
    } catch (err) {
      toast.error(err instanceof Error ? err.message : String(err));
    } finally {
      setBusy(false);
    }
  }, [archive.id, text, onChanged]);

  return (
    <>
      <button type="button" className="btn btn--small" onClick={openEditor}>
        ✎ {t('detail.editTags')}
      </button>
      <Modal open={open} onClose={() => setOpen(false)} title={t('detail.editTags')} wide>
        <div className="tag-editor">
          <textarea
            className="tag-editor__area"
            spellCheck={false}
            rows={14}
            value={text}
            onChange={(e) => setText(e.target.value)}
            aria-label={t('detail.editTags')}
          />
          <p className="tag-editor__hint">{t('detail.editTags.hint')}</p>
          <div className="fetch-meta__actions">
            <button
              type="button"
              className="btn btn--primary"
              disabled={busy}
              onClick={save}
            >
              {busy ? '…' : t('common.save')}
            </button>
            <button type="button" className="btn" onClick={() => setOpen(false)}>
              {t('common.cancel')}
            </button>
          </div>
        </div>
      </Modal>
    </>
  );
}

/** Lazy preview thumbnail grid using ?page=N thumbnails. */
function PreviewGrid({ archive }: { archive: Archive }) {
  // Cap the preview to a reasonable number; reader covers the full read.
  const count = Math.min(archive.page_count, 24);
  if (count <= 0) return null;
  return (
    <section className="detail__section">
      <h2>{t('detail.preview')}</h2>
      <div className="preview-grid">
        {Array.from({ length: count }, (_, i) => (
          <Link key={i} to={`/read/${archive.id}?page=${i}`} className="preview-grid__item">
            <SmartImage
              src={thumbnailUrl(archive.id, i)}
              alt={`Page ${i + 1}`}
              loading="lazy"
              className="preview-grid__img"
            />
            <span className="preview-grid__num">{i + 1}</span>
          </Link>
        ))}
      </div>
    </section>
  );
}

function CommentsSection({ archive }: { archive: Archive }) {
  const comments = archive.comments ?? [];
  return (
    <section className="detail__section">
      <h2>
        {t('detail.comments')} {comments.length > 0 && `(${comments.length})`}
      </h2>
      {comments.length === 0 ? (
        <p className="detail__muted">{t('detail.noComments')}</p>
      ) : (
        <ul className="comments">
          {comments.map((c, i) => (
            <li key={i} className="comment">
              <div className="comment__head">
                <span className="comment__user">{c.username}</span>
                <span className="comment__time">{formatTime(c.posted_at)}</span>
              </div>
              <div className="comment__body">{c.content}</div>
            </li>
          ))}
        </ul>
      )}
    </section>
  );
}

/** Best-effort similar / same-series via tag-based search; may be empty. */
function RelatedSections({ archive }: { archive: Archive }) {
  const seriesTag = archive.tags.find(
    (tag) => tag.namespace === 'parody' || tag.namespace === 'series',
  );
  const artistTag = archive.tags.find((tag) => tag.namespace === 'artist');

  const series = useAsync<Archive[]>(
    async (signal) => {
      if (!seriesTag) return [];
      const res = await listArchives({ q: tagToken(seriesTag), page: 1 }, signal);
      return res.archives.filter((a) => a.id !== archive.id).slice(0, 8);
    },
    [archive.id, seriesTag?.value],
  );

  const similar = useAsync<Archive[]>(
    async (signal) => {
      if (!artistTag) return [];
      const res = await listArchives({ q: tagToken(artistTag), page: 1 }, signal);
      return res.archives.filter((a) => a.id !== archive.id).slice(0, 8);
    },
    [archive.id, artistTag?.value],
  );

  return (
    <>
      <RelatedRow title={t('detail.sameSeries')} items={series.data ?? []} />
      <RelatedRow title={t('detail.similar')} items={similar.data ?? []} />
    </>
  );
}

function RelatedRow({ title, items }: { title: string; items: Archive[] }) {
  if (items.length === 0) return null;
  return (
    <section className="detail__section">
      <h2>{title}</h2>
      <div className="related-row">
        {items.map((a) => (
          <Link key={a.id} to={`/archive/${a.id}`} className="related-row__item" title={a.title}>
            <SmartImage
              src={thumbnailUrl(a.id)}
              alt={a.title}
              loading="lazy"
              className="related-row__img"
            />
            <span className="related-row__title">{a.title}</span>
          </Link>
        ))}
      </div>
    </section>
  );
}

/**
 * Owner action: fetch title/tags/rating/comments from a Metadata plugin
 * (nhentai / hitomi / e-hentai) — the rescue flow for archives whose source
 * gallery is gone. Results surface as a toast.
 */
function FetchMetadataButton({
  archive,
  onChanged,
}: {
  archive: Archive;
  onChanged: () => void;
}) {
  const [open, setOpen] = useState(false);
  const [namespace, setNamespace] = useState('');
  const [url, setUrl] = useState('');
  const [mode, setMode] = useState<'merge' | 'replace'>('merge');
  const [setTitle, setSetTitle] = useState(true);
  const [busy, setBusy] = useState(false);

  const { data } = useAsync<{ plugins: PluginInfo[] }>(
    async (signal) => (open ? listPlugins(signal) : { plugins: [] }),
    [open],
  );
  const metaPlugins = useMemo(
    () => (data?.plugins ?? []).filter((p) => p.type === 'metadata'),
    [data?.plugins],
  );

  // Default to the first metadata plugin once the list arrives.
  const selected = namespace || metaPlugins[0]?.namespace || '';

  const run = useCallback(async () => {
    if (!selected) return;
    setBusy(true);
    try {
      const res = await runMetadataPlugin(selected, {
        archiveId: archive.id,
        url: url.trim() || undefined,
        mode,
        setTitle,
      });
      const parts: string[] = [];
      parts.push(
        res.added_tags.length > 0
          ? `${res.added_tags.length} ${t('detail.fetchMeta.added')}`
          : t('detail.fetchMeta.noNew'),
      );
      if (res.title_applied && res.new_title) parts.push(`“${res.new_title}”`);
      if (res.rating != null) parts.push(`★ ${res.rating.toFixed(2)}`);
      if (res.comment_count) parts.push(`${res.comment_count} ${t('detail.fetchMeta.comments')}`);
      toast.success(parts.join(' · '));
      setOpen(false);
      onChanged();
    } catch (err) {
      toast.error(err instanceof Error ? err.message : String(err));
    } finally {
      setBusy(false);
    }
  }, [selected, archive.id, url, mode, setTitle, onChanged]);

  return (
    <>
      <button type="button" className="btn" onClick={() => setOpen(true)}>
        ⤵ {t('detail.fetchMeta')}
      </button>
      <Modal open={open} onClose={() => setOpen(false)} title={t('detail.fetchMeta')}>
        <div className="fetch-meta">
          <label className="fetch-meta__field">
            <span>{t('detail.fetchMeta.plugin')}</span>
            <select
              className="select"
              value={selected}
              onChange={(e) => setNamespace(e.target.value)}
            >
              {metaPlugins.map((p) => (
                <option key={p.namespace} value={p.namespace}>
                  {p.name}
                </option>
              ))}
            </select>
          </label>

          <label className="fetch-meta__field">
            <span>{t('detail.fetchMeta.url')}</span>
            <input
              type="text"
              placeholder="https://nhentai.net/g/…"
              aria-label={t('detail.fetchMeta.url')}
              value={url}
              onChange={(e) => setUrl(e.target.value)}
            />
            <span className="fetch-meta__hint">{t('detail.fetchMeta.urlHint')}</span>
          </label>

          <label className="fetch-meta__field">
            <span>{t('detail.fetchMeta.mode')}</span>
            <select
              className="select"
              value={mode}
              onChange={(e) => setMode(e.target.value as 'merge' | 'replace')}
            >
              <option value="merge">{t('detail.fetchMeta.merge')}</option>
              <option value="replace">{t('detail.fetchMeta.replace')}</option>
            </select>
          </label>

          <label className="fetch-meta__check">
            <input
              type="checkbox"
              checked={setTitle}
              onChange={(e) => setSetTitle(e.target.checked)}
            />
            <span>{t('detail.fetchMeta.setTitle')}</span>
          </label>

          <div className="fetch-meta__actions">
            <button
              type="button"
              className="btn btn--primary"
              disabled={busy || !selected}
              onClick={run}
            >
              {busy ? t('detail.fetchMeta.running') : t('detail.fetchMeta.run')}
            </button>
            <button type="button" className="btn" onClick={() => setOpen(false)}>
              {t('common.close')}
            </button>
          </div>
          {busy && <Spinner label={t('detail.fetchMeta.running')} />}
        </div>
      </Modal>
    </>
  );
}

function formatTime(iso: string): string {
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return iso;
  return d.toLocaleString();
}
