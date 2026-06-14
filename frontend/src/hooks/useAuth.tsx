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
import { lsGet } from '../lib/storage';
import type { Role } from '../api/types';

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
    setCredentials(res.api_key, res.role);
    setRole(res.role);
    setUnlocked(true);
  }, []);

  // Auto-lock on idle. Re-armed on each user-activity event.
  useEffect(() => {
    if (!unlocked) return;
    const minutes = lsGet<number>(AUTOLOCK_KEY, 0);
    if (!minutes || minutes <= 0) return;

    const ms = minutes * 60 * 1000;
    const arm = () => {
      window.clearTimeout(idleTimer.current);
      idleTimer.current = window.setTimeout(lock, ms);
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
