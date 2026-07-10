// High-level, typed wrappers around each REST endpoint in spec §6.1.
// Components and hooks call these, never `fetch` directly.

import {
  apiDelete,
  apiGet,
  apiPost,
  apiPut,
  bearerToken,
  mediaUrl,
  notifyUnauthorized,
} from './client';
import type {
  AppConfig,
  Archive,
  ArchiveListResponse,
  BalanceResponse,
  Category,
  JobListResponse,
  LoginResponse,
  PagesResponse,
  PluginInfo,
  RunPluginResponse,
  TagStat,
} from './types';

// ---- Auth -----------------------------------------------------------------

export function login(passcode: string) {
  return apiPost<LoginResponse>('/api/auth/login', {
    json: { passcode },
    noAuth: true,
  });
}

export function changePasscode(newPasscode: string, currentPasscode?: string) {
  return apiPost<{ ok: boolean; role: string }>('/api/auth/passcode', {
    json: { new_passcode: newPasscode, current_passcode: currentPasscode },
  });
}

/** First-run status (owner_configured + first_run window flag). Unauthenticated. */
export function getAuthStatus(signal?: AbortSignal) {
  return apiGet<{ owner_configured: boolean; first_run: boolean }>('/api/auth/status', {
    signal,
    noAuth: true,
  });
}

/** First-run choice: keep the provisioned passcode, or regenerate (returns it once). */
export function firstRunChoice(regenerate: boolean) {
  return apiPost<{ ok: boolean; regenerated: boolean; passcode?: string }>(
    '/api/auth/first-run',
    { json: { regenerate }, noAuth: true },
  );
}

// ---- Library --------------------------------------------------------------

export interface ArchiveQuery {
  q?: string;
  category?: string;
  sort?: SortKey;
  sortdir?: 'asc' | 'desc';
  page?: number;
  /** Sugar for the `newonly` query filter token (unread archives only). */
  newonly?: boolean;
  /** Allow passing the query object generically to apiGet without widening at each call site. */
  [key: string]: string | number | boolean | null | undefined;
}

export type SortKey = 'title' | 'date_added' | 'lastread';

export function listArchives(query: ArchiveQuery, signal?: AbortSignal) {
  return apiGet<ArchiveListResponse>('/api/archives', { query, signal });
}

export function getArchive(id: string, signal?: AbortSignal) {
  return apiGet<Archive>(`/api/archives/${encodeURIComponent(id)}`, { signal });
}

/** A random new (unread) archive when one exists, else any random archive. */
export function randomArchive(signal?: AbortSignal) {
  return apiGet<{ id: string; new: boolean }>('/api/archives/random', { signal });
}

export function getPages(id: string, signal?: AbortSignal) {
  return apiGet<PagesResponse>(`/api/archives/${encodeURIComponent(id)}/pages`, {
    signal,
  });
}

/** PUT metadata body — tags are raw `namespace:value` strings (full replacement). */
export interface MetadataPatch {
  title?: string;
  summary?: string;
  rating?: number;
  language?: string;
  tags?: string[];
}

export function updateMetadata(id: string, patch: MetadataPatch) {
  return apiPut<Archive>(`/api/archives/${encodeURIComponent(id)}/metadata`, {
    json: patch,
  });
}

export function setRating(id: string, rating: number) {
  return updateMetadata(id, { rating });
}

/** Deletes the archive record AND the file on disk (plus sidecars). */
export function deleteArchive(id: string) {
  return apiDelete<void>(`/api/archives/${encodeURIComponent(id)}`, {
    query: { delete_file: true },
  });
}

export function setProgress(id: string, page: number) {
  return apiPut<void>(
    `/api/archives/${encodeURIComponent(id)}/progress/${page}`,
  );
}

// ---- Media URLs (for <img> elements) --------------------------------------

/** Cover thumbnail (no page param) or a specific page-grid thumbnail. */
export function thumbnailUrl(id: string, page?: number): string {
  return mediaUrl(`/api/archives/${encodeURIComponent(id)}/thumbnail`, {
    page,
  });
}

/** Full-resolution page image. `path` is the archive-relative path. */
export function pageImageUrl(id: string, path: string): string {
  return mediaUrl(`/api/archives/${encodeURIComponent(id)}/page`, { path });
}

/** Download URL for the original, unmodified archive file. */
export function archiveDownloadUrl(id: string): string {
  return mediaUrl(`/api/archives/${encodeURIComponent(id)}/download`);
}

// ---- Tags -----------------------------------------------------------------

export function getTagStats(signal?: AbortSignal) {
  return apiGet<{ tags: TagStat[] }>('/api/tags/stats', { signal });
}

// ---- Categories -----------------------------------------------------------

export function listCategories(signal?: AbortSignal) {
  return apiGet<{ categories: Category[] }>('/api/categories', { signal });
}

export function createCategory(name: string, predicate?: string) {
  return apiPost<Category>('/api/categories', {
    json: { name, predicate, type: predicate ? 'dynamic' : 'static' },
  });
}

export function addToCategory(categoryId: string, archiveId: string) {
  return apiPut<void>(
    `/api/categories/${encodeURIComponent(categoryId)}/${encodeURIComponent(archiveId)}`,
  );
}

export function removeFromCategory(categoryId: string, archiveId: string) {
  return apiDelete<void>(
    `/api/categories/${encodeURIComponent(categoryId)}/${encodeURIComponent(archiveId)}`,
  );
}

// ---- Favorites (simple boolean toggle) --------------------------------------

export function setFavorite(archiveId: string, favorite: boolean) {
  const path = `/api/archives/${encodeURIComponent(archiveId)}/favorite`;
  return favorite
    ? apiPut<{ archive_id: string; favorite: boolean }>(path)
    : apiDelete<{ archive_id: string; favorite: boolean }>(path);
}

// ---- Downloads ------------------------------------------------------------

export function submitDownload(url: string, catid?: string) {
  return apiPost<{ id: string }>('/api/download', { json: { url, catid } });
}

export function listJobs(signal?: AbortSignal) {
  return apiGet<JobListResponse>('/api/jobs', { signal });
}

export function setJobPriority(id: string, priority: number) {
  return apiPost<void>(`/api/job/${encodeURIComponent(id)}/priority`, {
    json: { priority },
  });
}

export function getBalance(url?: string, signal?: AbortSignal) {
  // With no URL the server returns just the account GP balance (no per-gallery cost).
  return apiGet<BalanceResponse>('/api/ehentai/balance', {
    query: url && url.trim() ? { url: url.trim() } : {},
    signal,
  });
}

// ---- Upload ---------------------------------------------------------------

export function uploadArchive(file: File, onProgress?: (frac: number) => void) {
  // Use XHR for upload progress events (fetch lacks upload progress).
  return new Promise<{ id?: string }>((resolve, reject) => {
    const form = new FormData();
    form.append('file', file, file.name);
    const xhr = new XMLHttpRequest();
    xhr.open('POST', '/api/upload');
    // Auth comes from the shared client module (same key store + encoding).
    const token = bearerToken();
    if (token) xhr.setRequestHeader('Authorization', token);
    xhr.upload.onprogress = (e) => {
      if (e.lengthComputable && onProgress) onProgress(e.loaded / e.total);
    };
    xhr.onload = () => {
      if (xhr.status >= 200 && xhr.status < 300) {
        try {
          resolve(xhr.responseText ? JSON.parse(xhr.responseText) : {});
        } catch {
          resolve({});
        }
      } else if (xhr.status === 401) {
        // Bounce to the lock screen like every other 401.
        notifyUnauthorized();
        reject(new Error('Unauthorized'));
      } else {
        reject(new Error(`Upload failed (${xhr.status})`));
      }
    };
    xhr.onerror = () => reject(new Error('Upload failed (network error)'));
    xhr.send(form);
  });
}

// ---- Plugins --------------------------------------------------------------

export function listPlugins(signal?: AbortSignal) {
  return apiGet<{ plugins: PluginInfo[] }>('/api/plugins', { signal });
}

export function setPluginConfig(namespace: string, values: Record<string, unknown>) {
  // The backend expects the values wrapped under a `values` key (PluginConfigBody).
  return apiPost<{ namespace: string; saved: string[] }>(
    `/api/plugins/${encodeURIComponent(namespace)}/config`,
    { json: { values } },
  );
}

/** Run a metadata plugin against an archive (rescue flow for dead source galleries). */
export function runMetadataPlugin(
  namespace: string,
  options: {
    archiveId: string;
    url?: string;
    mode?: 'merge' | 'replace';
    setTitle?: boolean;
  },
) {
  return apiPost<RunPluginResponse>(`/api/plugins/${encodeURIComponent(namespace)}/run`, {
    json: {
      archive_id: options.archiveId,
      url: options.url || undefined,
      mode: options.mode ?? 'merge',
      set_title: options.setTitle,
    },
  });
}

// ---- Admin ----------------------------------------------------------------

export function getConfig(signal?: AbortSignal) {
  return apiGet<AppConfig>('/api/config', { signal });
}

export function updateConfig(config: AppConfig) {
  return apiPut<AppConfig>('/api/config', { json: config });
}

export function scanLibrary() {
  return apiPost<{ ok?: boolean }>('/api/library/scan');
}

export function regenThumbnails() {
  return apiPost<{ ok?: boolean }>('/api/thumbnails/regen');
}

export function prewarmThumbnails() {
  return apiPost<{ started?: boolean }>('/api/thumbnails/prewarm');
}
