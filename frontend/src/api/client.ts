// Low-level API client.
//
// Auth model (spec §6.1): the API key is sent as
//   Authorization: Bearer <base64(apiKey)>
// on every request. This module owns the base64 encoding, the stored key, and
// the 401 handling. A registered `onUnauthorized` callback lets the app return
// to the passcode screen when the server rejects the key.

import { lsGetRaw, lsRemove, lsSetRaw } from '../lib/storage';
import type { Role } from './types';

const API_BASE = '/api';
const KEY_STORAGE = 'apiKey';
const ROLE_STORAGE = 'role';

/** UTF-8 safe base64 (btoa alone breaks on multibyte api keys). */
export function base64(input: string): string {
  const bytes = new TextEncoder().encode(input);
  let binary = '';
  for (const b of bytes) binary += String.fromCharCode(b);
  return btoa(binary);
}

let apiKey: string | null = lsGetRaw(KEY_STORAGE);
let role: Role | null = (lsGetRaw(ROLE_STORAGE) as Role | null) ?? null;
let onUnauthorized: (() => void) | null = null;

export function setUnauthorizedHandler(fn: () => void): void {
  onUnauthorized = fn;
}

export function getApiKey(): string | null {
  return apiKey;
}

export function getRole(): Role | null {
  return role;
}

export function isOwner(): boolean {
  return role === 'owner';
}

export function setCredentials(key: string, r: Role): void {
  apiKey = key;
  role = r;
  lsSetRaw(KEY_STORAGE, key);
  lsSetRaw(ROLE_STORAGE, r);
}

/** Clear the stored key — used by lock / auto-lock / 401. */
export function clearCredentials(): void {
  apiKey = null;
  role = null;
  lsRemove(KEY_STORAGE);
  lsRemove(ROLE_STORAGE);
}

export class ApiError extends Error {
  status: number;
  body: unknown;
  constructor(status: number, message: string, body?: unknown) {
    super(message);
    this.name = 'ApiError';
    this.status = status;
    this.body = body;
  }
}

function authHeader(): Record<string, string> {
  if (!apiKey) return {};
  return { Authorization: `Bearer ${base64(apiKey)}` };
}

interface RequestOptions {
  method?: string;
  /** JSON body — serialized automatically. */
  json?: unknown;
  /** Raw body (e.g. FormData) — sent as-is, no content-type forced. */
  body?: BodyInit;
  query?: Record<string, string | number | boolean | undefined | null>;
  signal?: AbortSignal;
  /** Skip the auth header (used by the login endpoint). */
  noAuth?: boolean;
}

function buildUrl(path: string, query?: RequestOptions['query']): string {
  const url = path.startsWith('/api') ? path : API_BASE + path;
  if (!query) return url;
  const params = new URLSearchParams();
  for (const [k, v] of Object.entries(query)) {
    if (v === undefined || v === null || v === '') continue;
    params.set(k, String(v));
  }
  const qs = params.toString();
  return qs ? `${url}?${qs}` : url;
}

async function request<T>(path: string, opts: RequestOptions = {}): Promise<T> {
  const headers: Record<string, string> = {};
  if (!opts.noAuth) Object.assign(headers, authHeader());

  let body: BodyInit | undefined = opts.body;
  if (opts.json !== undefined) {
    headers['Content-Type'] = 'application/json';
    body = JSON.stringify(opts.json);
  }

  let res: Response;
  try {
    res = await fetch(buildUrl(path, opts.query), {
      method: opts.method ?? 'GET',
      headers,
      body,
      signal: opts.signal,
    });
  } catch (err) {
    if ((err as Error).name === 'AbortError') throw err;
    throw new ApiError(0, 'Network error — is the server reachable?');
  }

  if (res.status === 401) {
    // Key is invalid/expired. Drop it and bounce to the passcode screen.
    clearCredentials();
    onUnauthorized?.();
    throw new ApiError(401, 'Unauthorized');
  }

  if (!res.ok) {
    let detail: unknown = undefined;
    let message = `Request failed (${res.status})`;
    try {
      detail = await res.json();
      const d = detail as { detail?: string; error?: string; message?: string };
      message = d.detail ?? d.error ?? d.message ?? message;
    } catch {
      try {
        message = (await res.text()) || message;
      } catch {
        /* ignore */
      }
    }
    throw new ApiError(res.status, message, detail);
  }

  if (res.status === 204) return undefined as T;
  const contentType = res.headers.get('content-type') ?? '';
  if (contentType.includes('application/json')) {
    return (await res.json()) as T;
  }
  // Non-JSON success (rare here) — return text.
  return (await res.text()) as unknown as T;
}

export const apiGet = <T>(path: string, opts?: Omit<RequestOptions, 'method' | 'json' | 'body'>) =>
  request<T>(path, { ...opts, method: 'GET' });

export const apiPost = <T>(path: string, opts?: Omit<RequestOptions, 'method'>) =>
  request<T>(path, { ...opts, method: 'POST' });

export const apiPut = <T>(path: string, opts?: Omit<RequestOptions, 'method'>) =>
  request<T>(path, { ...opts, method: 'PUT' });

export const apiDelete = <T>(path: string, opts?: Omit<RequestOptions, 'method'>) =>
  request<T>(path, { ...opts, method: 'DELETE' });

/**
 * Build an authenticated URL for an <img> tag. Browsers can't set headers on
 * <img src>, so for media (thumbnails/pages) we pass the key as a query param
 * the backend also accepts. The Bearer header remains the canonical path for
 * fetch() calls; this is the documented fallback for media elements.
 *
 * Assumption for the backend: GET media endpoints accept `?key=<base64(apiKey)>`
 * in addition to the Authorization header.
 */
export function mediaUrl(
  path: string,
  query?: Record<string, string | number | undefined | null>,
): string {
  const merged: Record<string, string | number | undefined | null> = { ...query };
  if (apiKey) merged.key = base64(apiKey);
  return buildUrl(path, merged);
}
