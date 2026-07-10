// Passcode entry / lock screen. Shown whenever the app is locked (no key,
// after lock/auto-lock, or after a 401). On a fresh install (first run) it
// also offers to keep the provisioned passcode or generate a new one in the
// browser — for installs without terminal access (e.g. Docker).

import { useState, type FormEvent } from 'react';
import { useAuth } from '../hooks/useAuth';
import { ApiError } from '../api/client';
import { firstRunChoice, getAuthStatus } from '../api/endpoints';
import { useAsync } from '../hooks/useApi';
import { useTheme } from '../hooks/useTheme';
import { t } from '../i18n/strings';

export function LockScreen() {
  const { login } = useAuth();
  const { theme, toggle } = useTheme();
  const [passcode, setPasscode] = useState('');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function onSubmit(e: FormEvent) {
    e.preventDefault();
    if (busy || !passcode) return;
    setBusy(true);
    setError(null);
    try {
      await login(passcode);
      setPasscode('');
    } catch (err) {
      if (err instanceof ApiError && err.status === 401) {
        setError(t('auth.error'));
      } else if (err instanceof ApiError && err.status === 0) {
        setError(err.message);
      } else {
        setError(err instanceof Error ? err.message : t('common.error'));
      }
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="lock-screen">
      <button
        type="button"
        className="btn btn--icon lock-screen__theme"
        onClick={toggle}
        aria-label="Toggle theme"
      >
        {theme === 'dark' ? '☀' : '☾'}
      </button>
      <form className="lock-card" onSubmit={onSubmit}>
        <div className="lock-card__logo" aria-hidden>
          📚
        </div>
        <h1 className="lock-card__title">{t('auth.title')}</h1>
        <p className="lock-card__subtitle">{t('auth.subtitle')}</p>
        <input
          type="password"
          className="lock-card__input"
          placeholder={t('auth.passcode')}
          value={passcode}
          onChange={(e) => setPasscode(e.target.value)}
          autoFocus
          autoComplete="current-password"
          aria-label={t('auth.passcode')}
        />
        {error && <div className="lock-card__error">{error}</div>}
        <button type="submit" className="btn btn--primary lock-card__submit" disabled={busy}>
          {busy ? t('auth.unlocking') : t('auth.unlock')}
        </button>
      </form>
      <FirstRunPanel />
    </div>
  );
}

/**
 * First-run affordance: while the server-side first-run window is open, the
 * user can keep the passcode printed at init time, or mint a new one and see
 * it here exactly once. The window closes on the first login or choice.
 */
function FirstRunPanel() {
  const { data } = useAsync(
    (signal) => getAuthStatus(signal).catch(() => null),
    [],
  );
  const [resolved, setResolved] = useState(false);
  const [generated, setGenerated] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  if (!data?.first_run || (resolved && !generated)) {
    if (generated) {
      return (
        <div className="lock-card lock-card--firstrun">
          <h2 className="lock-card__subtitle">{t('auth.firstRun.generated')}</h2>
          <code className="lock-card__passcode">{generated}</code>
          <p className="lock-card__subtitle">{t('auth.firstRun.done')}</p>
        </div>
      );
    }
    return null;
  }

  const choose = async (regenerate: boolean) => {
    setBusy(true);
    setError(null);
    try {
      const res = await firstRunChoice(regenerate);
      setResolved(true);
      if (res.regenerated && res.passcode) setGenerated(res.passcode);
    } catch (err) {
      setError(err instanceof Error ? err.message : t('common.error'));
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="lock-card lock-card--firstrun">
      <h2 className="lock-card__title">{t('auth.firstRun.title')}</h2>
      <p className="lock-card__subtitle">{t('auth.firstRun.body')}</p>
      <div className="lock-card__firstrun-actions">
        <button type="button" className="btn" disabled={busy} onClick={() => choose(false)}>
          {t('auth.firstRun.keep')}
        </button>
        <button
          type="button"
          className="btn btn--primary"
          disabled={busy}
          onClick={() => choose(true)}
        >
          {t('auth.firstRun.new')}
        </button>
      </div>
      {error && <div className="lock-card__error">{error}</div>}
    </div>
  );
}
