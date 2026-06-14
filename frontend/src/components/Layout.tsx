// App shell: top nav bar + routed content. The Reader renders without this
// chrome (it manages its own full-bleed UI), so Layout is applied per-route
// rather than wrapping the router.

import { NavLink, Outlet } from 'react-router-dom';
import { useAuth } from '../hooks/useAuth';
import { useTheme } from '../hooks/useTheme';
import { t } from '../i18n/strings';

export function Layout() {
  const { lock, isOwner, role } = useAuth();
  const { theme, toggle } = useTheme();

  return (
    <div className="app-shell">
      <header className="navbar">
        <NavLink to="/" className="navbar__brand">
          <span className="navbar__logo" aria-hidden>📚</span>
          <span>{t('app.name')}</span>
        </NavLink>
        <nav className="navbar__links">
          <NavLink to="/" end className="navbar__link">
            {t('nav.library')}
          </NavLink>
          {isOwner && (
            <>
              <NavLink to="/downloads" className="navbar__link">
                {t('nav.downloads')}
              </NavLink>
              <NavLink to="/settings" className="navbar__link">
                {t('nav.settings')}
              </NavLink>
            </>
          )}
        </nav>
        <div className="navbar__actions">
          <span className="navbar__role" title="Current role">
            {role === 'owner' ? t('common.owner') : t('common.reader')}
          </span>
          <button
            type="button"
            className="btn btn--icon"
            onClick={toggle}
            aria-label="Toggle theme"
            title="Toggle theme"
          >
            {theme === 'dark' ? '☀' : '☾'}
          </button>
          <button
            type="button"
            className="btn btn--icon"
            onClick={lock}
            aria-label={t('nav.lock')}
            title={t('nav.lock')}
          >
            🔒
          </button>
        </div>
      </header>
      <main className="app-main">
        <Outlet />
      </main>
    </div>
  );
}
