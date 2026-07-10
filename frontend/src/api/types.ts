// API response types for the MangaCouch REST surface.
//
// These mirror §3.2 (data model) and §6.1 (REST API surface) of the design
// spec. Where the spec leaves a response shape implicit, the assumed shape is
// documented inline so the backend can match it. Fields are kept permissive
// (many optional) because the backend is still under construction.

export type Role = 'owner';

/** Server-configured client defaults (config.toml [reader]/[auth]) sent with login.
 * Used only to seed local preferences that the user has never set. */
export interface ClientDefaults {
  reader?: {
    mode?: string;
    direction?: string;
    fit?: string;
    preload?: number;
  };
  theme?: string;
  language?: string;
  auto_lock_minutes?: number;
}

export interface LoginResponse {
  api_key: string;
  role: Role;
  defaults?: ClientDefaults;
}

/** A single namespaced tag. `translated` is the localized display string. */
export interface Tag {
  namespace: string;
  value: string;
  /** Localized display name from EhTagTranslation, if available. */
  translated?: string | null;
}

/** Reading progress as serialized by the backend (serialization.py). */
export interface Progress {
  /** Last-read page (0-based). */
  page: number;
  /** page / page_count clamped to [0, 1]. */
  percent: number;
}

/**
 * An archive (one manga/gallery). Mirrors the `archive` table plus joined
 * tags and progress. `id` is the xxh3-128 hex content hash.
 */
export interface Archive {
  id: string;
  title: string;
  original_filename?: string | null;
  summary?: string | null;
  page_count: number;
  format?: string | null;
  size?: number | null;
  rating?: number | null;
  added_at?: string | null;
  source_url?: string | null;
  source_gid?: number | string | null;
  source_token?: string | null;
  tags: Tag[];
  /** Reading progress object (`{page, percent}`) from the `progress` table. */
  progress?: Progress | null;
  /** Server-computed "read" flag (progress.percent > 0.85). */
  read?: boolean;
  /** Engagement counts (best-effort; may be absent). */
  love_count?: number | null;
  view_count?: number | null;
  favorite_count?: number | null;
  /** Simple favorite flag (detail responses only). */
  favorite?: boolean;
  /** Source-site gallery comments. */
  comments?: Comment[];
}

/** Listing response for GET /api/archives. */
export interface ArchiveListResponse {
  archives: Archive[];
  total: number;
  page: number;
  /** Page size used by the server (assumed; falls back to count). */
  page_size?: number;
  /** Total number of pages, if the server computes it. */
  total_pages?: number;
}

/** One page descriptor from GET /api/archives/{id}/pages. */
export interface PageDescriptor {
  index: number;
  /** Archive-relative path used as the `path` query param to fetch the image. */
  path: string;
  /** Optional intrinsic dimensions, if the server provides them. */
  width?: number | null;
  height?: number | null;
}

/** Response for GET /api/archives/{id}/pages. */
export interface PagesResponse {
  pages: PageDescriptor[];
}

export interface Comment {
  username: string;
  /** ISO timestamp. */
  posted_at: string;
  content: string;
}

export interface Category {
  id: string;
  name: string;
  type: 'static' | 'dynamic';
  predicate?: string | null;
  pinned?: boolean;
}

export interface FavoriteList {
  id: string;
  name: string;
  position?: number;
  /** IDs of archives in this list, if the server includes them. */
  archive_ids?: string[];
}

export type DownloadJobState =
  | 'queued'
  | 'running'
  | 'preparing'
  | 'done'
  | 'failed';

export interface DownloadJob {
  id: string;
  url: string;
  gid?: number | string | null;
  token?: string | null;
  domain?: string | null;
  dltype?: 'org' | 'res' | null;
  state: DownloadJobState;
  priority: number;
  /** 0..1 or 0..100; the UI treats >1 as a percentage. */
  progress?: number | null;
  gp_cost?: number | null;
  error?: string | null;
  next_run?: string | null;
  created_at?: string | null;
  /** Title once resolved. */
  title?: string | null;
  /** Resulting archive id once ingested. */
  archive_id?: string | null;
}

export interface JobListResponse {
  jobs: DownloadJob[];
}

/** GET /api/ehentai/balance?url= — the GP balance calculator. */
export interface BalanceResponse {
  /** Current account GP balance. */
  balance?: number | null;
  /** GP cost of the Original archive. */
  original_cost?: number | null;
  /** GP cost of the Resample archive (usually free/cheap). */
  resample_cost?: number | null;
  /** Whether the account can afford the Original archive. */
  sufficient?: boolean | null;
  gallery_title?: string | null;
  error?: string | null;
}

export interface PluginParam {
  name: string;
  type: string; // string | int | bool | password
  description?: string;
  default?: unknown;
  secret?: boolean;
}

export interface PluginInfo {
  namespace: string;
  name: string;
  type: 'login' | 'download' | 'metadata' | 'script';
  description?: string | null;
  version?: string | null;
  author?: string | null;
  cooldown?: number;
  login_from?: string | null;
  parameters?: PluginParam[];
  /** Stored config values; secret values arrive masked as "••••••••". */
  config?: Record<string, string>;
}

/** POST /api/plugins/{namespace}/run — run a metadata plugin against an archive. */
export interface RunPluginResponse {
  namespace: string;
  title_applied: boolean;
  new_title?: string | null;
  added_tags: string[];
  rating?: number | null;
  comment_count?: number;
  archive: Archive;
}

/** GET /api/tags/stats — tag cloud. */
export interface TagStat {
  namespace: string;
  value: string;
  count: number;
  translated?: string | null;
}

/** Centralized typed settings from GET/PUT /api/config. */
export interface AppConfig {
  [key: string]: unknown;
}
