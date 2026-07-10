// Auth context: holds the unlocked state, performs login, handles lock /
// auto-lock, and registers the 401 handler so any failed request bounces the
// app back to the passcode screen.

import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useRef,
  useState,
  type ReactNode,
} from 'react';
import {
  clearCredentials,
  getApiKey,
  getRole,
  setCredentials,
  setUnauthorizedHandler,
} from '../api/client';
import { login as loginRequest } from '../api/endpoints';
import { lsGet, lsGetRaw, lsSet } from '../lib/storage';
import { setLocale } from '../i18n/strings';
import type { ClientDefaults, Role } from '../api/types';

interface AuthState {
  unlocked: boolean;
  role: Role | null;
  isOwner: boolean;
  login: (passcode: string) => Promise<void>;
  lock: () => void;
}

const AuthContext = createContext<AuthState | null>(null);

/** Idle minutes before auto-lock; 0 = disabled. Stored by Settings. */
export const AUTOLOCK_KEY = 'autolockMinutes';

/** One-time flag: server client-defaults have been seeded into local prefs. */
const DEFAULTS_SEEDED_KEY = 'defaultsSeeded';

/**
 * Seed local preferences from the server's config.toml defaults, once, on the
 * first login on this device. Existing local values always win.
 */
function seedClientDefaults(defaults?: ClientDefaults): void {
  if (!defaults || lsGetRaw(DEFAULTS_SEEDED_KEY) != null) return;
  lsSet(DEFAULTS_SEEDED_KEY, true);

  if (typeof defaults.auto_lock_minutes === 'number' && lsGetRaw(AUTOLOCK_KEY) == null) {
    lsSet(AUTOLOCK_KEY, defaults.auto_lock_minutes);
  }

  const r = defaults.reader;
  if (r && lsGetRaw('readerSettings') == null) {
    const seed: Record<string, unknown> = {};
    if (r.mode === 'paged' || r.mode === 'scroll') seed.mode = r.mode;
    if (r.direction === 'ltr' || r.direction === 'rtl') seed.direction = r.direction;
    if (['width', 'height', 'container', 'original'].includes(r.fit ?? '')) seed.fit = r.fit;
    if (typeof r.preload === 'number') seed.preload = r.preload;
    lsSet('readerSettings', seed);
  }

  if (defaults.theme === 'dark' || defaults.theme === 'light') {
    lsSet('theme', defaults.theme);
    document.documentElement.setAttribute('data-theme', defaults.theme);
  }

  if (typeof defaults.language === 'string' && lsGetRaw('locale') == null) {
    setLocale(defaults.language.toLowerCase().startsWith('zh') ? 'zh-Hans' : 'en');
  }
}

const ACTIVITY_EVENTS = [
  'mousemove',
  'mousedown',
  'keydown',
  'touchstart',
  'scroll',
  'wheel',
] as const;

export function AuthProvider({ children }: { children: ReactNode }) {
  const [unlocked, setUnlocked] = useState<boolean>(() => !!getApiKey());
  const [role, setRole] = useState<Role | null>(() => getRole());
  const idleTimer = useRef<number | undefined>(undefined);

  const lock = useCallback(() => {
    clearCredentials();
    setUnlocked(false);
    setRole(null);
  }, []);

  // Register the global 401 handler exactly once.
  useEffect(() => {
    setUnauthorizedHandler(() => {
      setUnlocked(false);
      setRole(null);
    });
  }, []);

  const login = useCallback(async (passcode: string) => {
    const res = await loginRequest(passcode);
    setCredentials(res.api_key, res.role, res.media_key);
    seedClientDefaults(res.defaults);
    setRole(res.role);
    setUnlocked(true);
  }, []);

  // Auto-lock on idle. Re-armed on each user-activity event. The minutes value
  // is re-read on every re-arm so a change in Settings applies immediately —
  // not only after the next unlock.
  useEffect(() => {
    if (!unlocked) return;
    const arm = () => {
      window.clearTimeout(idleTimer.current);
      const minutes = lsGet<number>(AUTOLOCK_KEY, 0);
      if (!minutes || minutes <= 0) return;
      idleTimer.current = window.setTimeout(lock, minutes * 60 * 1000);
    };
    arm();
    for (const ev of ACTIVITY_EVENTS) {
      window.addEventListener(ev, arm, { passive: true });
    }
    return () => {
      window.clearTimeout(idleTimer.current);
      for (const ev of ACTIVITY_EVENTS) window.removeEventListener(ev, arm);
    };
  }, [unlocked, lock]);

  const value = useMemo<AuthState>(
    () => ({
      unlocked,
      role,
      isOwner: role === 'owner',
      login,
      lock,
    }),
    [unlocked, role, login, lock],
  );

  return <AuthContext.Provider value={value}>{children}</AuthContext.Provider>;
}

// eslint-disable-next-line react-refresh/only-export-components
export function useAuth(): AuthState {
  const ctx = useContext(AuthContext);
  if (!ctx) throw new Error('useAuth must be used within AuthProvider');
  return ctx;
}
