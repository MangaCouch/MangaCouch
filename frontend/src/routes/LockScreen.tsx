// Passcode entry / lock screen. Shown whenever the app is locked (no key,
// after lock/auto-lock, or after a 401).

import { useState, type FormEvent } from 'react';
import { useAuth } from '../hooks/useAuth';
import { ApiError } from '../api/client';
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
    </div>
  );
}
