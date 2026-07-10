// Settings / admin view (owner), organized into collapsible sections:
// Appearance · Security · Library & maintenance · Plugins · Advanced (raw
// config). Section open/closed state persists per user via <Collapsible>.

import { useCallback, useEffect, useMemo, useRef, useState, type FormEvent } from 'react';
import {
  changePasscode,
  getConfig,
  listPlugins,
  prewarmThumbnails,
  regenThumbnails,
  scanLibrary,
  setPluginConfig,
  updateConfig,
  uploadArchive,
} from '../api/endpoints';
import type { AppConfig, PluginInfo } from '../api/types';
import { useAsync } from '../hooks/useApi';
import { useTheme } from '../hooks/useTheme';
import { AUTOLOCK_KEY } from '../hooks/useAuth';
import { lsGet, lsSet } from '../lib/storage';
import { Spinner, ErrorBanner, Collapsible } from '../components/ui';
import { toast } from '../components/Toast';
import { getLocale, setLocale, t, type Locale } from '../i18n/strings';

const PLUGIN_TYPE_ORDER = ['metadata', 'download', 'login', 'script'] as const;

export function Settings() {
  return (
    <div className="settings">
      <header className="page-head">
        <h1>{t('settings.title')}</h1>
        <p className="page-head__sub">{t('settings.subtitle')}</p>
      </header>

      <Collapsible
        id="settings-appearance"
        icon="🎨"
        title={t('settings.section.appearance')}
        subtitle={t('settings.section.appearance.sub')}
        defaultOpen
      >
        <AppearancePanel />
      </Collapsible>

      <Collapsible
        id="settings-security"
        icon="🔐"
        title={t('settings.section.security')}
        subtitle={t('settings.section.security.sub')}
      >
        <SecurityPanel />
      </Collapsible>

      <Collapsible
        id="settings-library"
        icon="🗂️"
        title={t('settings.section.library')}
        subtitle={t('settings.section.library.sub')}
      >
        <AdminActions />
        <UploadPanel />
      </Collapsible>

      <Collapsible
        id="settings-plugins"
        icon="🧩"
        title={t('settings.plugins')}
        subtitle={t('settings.section.plugins.sub')}
      >
        <PluginsPanel />
      </Collapsible>

      <Collapsible
        id="settings-advanced"
        icon="⚙️"
        title={t('settings.section.advanced')}
        subtitle={t('settings.section.advanced.sub')}
      >
        <ConfigPanel />
      </Collapsible>
    </div>
  );
}

function AppearancePanel() {
  const { theme, setTheme } = useTheme();
  const [autolock, setAutolock] = useState<number>(() => lsGet<number>(AUTOLOCK_KEY, 0));
  const [locale, setLocaleState] = useState<Locale>(() => getLocale());

  const onAutolock = useCallback((minutes: number) => {
    setAutolock(minutes);
    lsSet(AUTOLOCK_KEY, minutes);
  }, []);

  const onLocale = useCallback((next: Locale) => {
    setLocaleState(next);
    setLocale(next);
    // Every t() call site re-evaluates on reload; simplest correct refresh.
    window.location.reload();
  }, []);

  return (
    <>
      <div className="field-row">
        <label className="settings__label">{t('settings.theme')}</label>
        <div className="segmented">
          <button
            type="button"
            className={`segmented__btn ${theme === 'dark' ? 'segmented__btn--active' : ''}`}
            onClick={() => setTheme('dark')}
          >
            {t('settings.theme.dark')}
          </button>
          <button
            type="button"
            className={`segmented__btn ${theme === 'light' ? 'segmented__btn--active' : ''}`}
            onClick={() => setTheme('light')}
          >
            {t('settings.theme.light')}
          </button>
        </div>
      </div>
      <div className="field-row">
        <label className="settings__label">{t('settings.language')}</label>
        <select
          className="select"
          value={locale}
          onChange={(e) => onLocale(e.target.value as Locale)}
          aria-label={t('settings.language')}
        >
          <option value="en">English</option>
          <option value="zh-Hans">简体中文</option>
        </select>
      </div>
      <div className="field-row">
        <label className="settings__label">{t('settings.autolock')}</label>
        <select
          className="select"
          value={autolock}
          onChange={(e) => onAutolock(Number(e.target.value))}
        >
          <option value={0}>{t('settings.autolock.off')}</option>
          {[1, 5, 15, 30, 60].map((m) => (
            <option key={m} value={m}>
              {m} {t('settings.minutes')}
            </option>
          ))}
        </select>
      </div>
    </>
  );
}

function SecurityPanel() {
  const [currentPass, setCurrentPass] = useState('');
  const [nextPass, setNextPass] = useState('');
  const [confirmPass, setConfirmPass] = useState('');
  const [busy, setBusy] = useState(false);

  const submit = useCallback(async () => {
    if (nextPass.length < 4) {
      toast.error(t('settings.passcode.tooShort'));
      return;
    }
    if (nextPass !== confirmPass) {
      toast.error(t('settings.passcode.mismatch'));
      return;
    }
    setBusy(true);
    try {
      await changePasscode(nextPass, currentPass);
      toast.success(t('settings.passcode.changed'));
      setCurrentPass('');
      setNextPass('');
      setConfirmPass('');
    } catch (err) {
      toast.error(err instanceof Error ? err.message : String(err));
    } finally {
      setBusy(false);
    }
  }, [nextPass, confirmPass, currentPass]);

  return (
    <form
      className="settings__form"
      onSubmit={(e) => {
        e.preventDefault();
        submit();
      }}
    >
      <input
        type="password"
        autoComplete="current-password"
        placeholder={t('settings.passcode.current')}
        aria-label={t('settings.passcode.current')}
        value={currentPass}
        onChange={(e) => setCurrentPass(e.target.value)}
      />
      <input
        type="password"
        autoComplete="new-password"
        placeholder={t('settings.passcode.new')}
        aria-label={t('settings.passcode.new')}
        value={nextPass}
        onChange={(e) => setNextPass(e.target.value)}
      />
      <input
        type="password"
        autoComplete="new-password"
        placeholder={t('settings.passcode.confirm')}
        aria-label={t('settings.passcode.confirm')}
        value={confirmPass}
        onChange={(e) => setConfirmPass(e.target.value)}
      />
      <button type="submit" className="btn btn--primary" disabled={busy}>
        {busy ? '…' : t('settings.passcode.update')}
      </button>
    </form>
  );
}

function AdminActions() {
  const [busy, setBusy] = useState<string | null>(null);

  const run = useCallback(async (label: string, fn: () => Promise<unknown>) => {
    setBusy(label);
    try {
      await fn();
      toast.success(`${label}: ${t('settings.started')}`);
    } catch (err) {
      toast.error(`${label}: ${err instanceof Error ? err.message : String(err)}`);
    } finally {
      setBusy(null);
    }
  }, []);

  return (
    <div className="settings__group">
      <h3 className="settings__subhead">{t('settings.maintenance')}</h3>
      <div className="settings__actions">
        <button
          type="button"
          className="btn"
          disabled={busy !== null}
          onClick={() => run(t('settings.scan'), scanLibrary)}
        >
          {busy === t('settings.scan') ? '…' : t('settings.scan')}
        </button>
        <button
          type="button"
          className="btn"
          disabled={busy !== null}
          onClick={() =>
            run(t('settings.regen'), async () => {
              await regenThumbnails();
              // Thumbnails are served CacheFirst by the SW — drop the cache so the
              // regenerated images (same URLs, new bytes) actually show up.
              if ('caches' in window) await caches.delete('mc-thumbnails');
            })
          }
        >
          {busy === t('settings.regen') ? '…' : t('settings.regen')}
        </button>
        <button
          type="button"
          className="btn"
          disabled={busy !== null}
          title={t('settings.prewarm.hint')}
          onClick={() => run(t('settings.prewarm'), prewarmThumbnails)}
        >
          {busy === t('settings.prewarm') ? '…' : t('settings.prewarm')}
        </button>
      </div>
      {busy && <Spinner label={busy} />}
    </div>
  );
}

function UploadPanel() {
  const [progress, setProgress] = useState<number | null>(null);
  const inputRef = useRef<HTMLInputElement | null>(null);

  const onFile = useCallback(async (file: File) => {
    setProgress(0);
    try {
      await uploadArchive(file, (f) => setProgress(f));
      setProgress(1);
      toast.success(t('settings.uploaded'));
    } catch (err) {
      toast.error(err instanceof Error ? err.message : String(err));
    } finally {
      setTimeout(() => setProgress(null), 800);
      if (inputRef.current) inputRef.current.value = '';
    }
  }, []);

  return (
    <div className="settings__group">
      <h3 className="settings__subhead">{t('settings.upload')}</h3>
      <input
        ref={inputRef}
        type="file"
        accept=".zip,.cbz,.pdf,application/zip,application/pdf"
        onChange={(e) => {
          const f = e.target.files?.[0];
          if (f) onFile(f);
        }}
      />
      {progress != null && (
        <div className="upload__progress">
          <div className="upload__bar" style={{ width: `${progress * 100}%` }} />
        </div>
      )}
    </div>
  );
}

function ConfigPanel() {
  const { data, error, loading, reload } = useAsync<AppConfig>(
    (signal) => getConfig(signal),
    [],
  );
  const [text, setText] = useState('');
  const [saveMsg, setSaveMsg] = useState<string | null>(null);
  const [saving, setSaving] = useState(false);

  // Populate the editor once config arrives.
  useEffect(() => {
    if (data) setText(JSON.stringify(data, null, 2));
  }, [data]);

  const onSave = useCallback(async () => {
    let parsed: AppConfig;
    try {
      parsed = JSON.parse(text);
    } catch {
      setSaveMsg('Invalid JSON.');
      return;
    }
    setSaving(true);
    setSaveMsg(null);
    try {
      await updateConfig(parsed);
      toast.success(t('settings.saved'));
      reload();
    } catch (err) {
      toast.error(err instanceof Error ? err.message : String(err));
    } finally {
      setSaving(false);
    }
  }, [text, reload]);

  return (
    <>
      <p className="settings__hint">{t('settings.advanced.warning')}</p>
      {loading && <Spinner />}
      {error && <ErrorBanner error={error} onRetry={reload} />}
      {!loading && !error && (
        <>
          <textarea
            className="config-editor"
            spellCheck={false}
            value={text}
            onChange={(e) => setText(e.target.value)}
            rows={14}
          />
          <div className="settings__actions">
            <button type="button" className="btn btn--primary" onClick={onSave} disabled={saving}>
              {saving ? '…' : t('settings.save')}
            </button>
          </div>
          {saveMsg && <div className="settings__msg">{saveMsg}</div>}
        </>
      )}
    </>
  );
}

function PluginsPanel() {
  const { data, error, loading, reload } = useAsync<{ plugins: PluginInfo[] }>(
    (signal) => listPlugins(signal),
    [],
  );
  const plugins = useMemo(() => data?.plugins ?? [], [data?.plugins]);

  const groups = useMemo(() => {
    const byType = new Map<string, PluginInfo[]>();
    for (const p of plugins) {
      const list = byType.get(p.type) ?? [];
      list.push(p);
      byType.set(p.type, list);
    }
    return PLUGIN_TYPE_ORDER.filter((type) => byType.has(type)).map(
      (type) => [type, byType.get(type)!] as const,
    );
  }, [plugins]);

  return (
    <>
      {loading && <Spinner />}
      {error && <ErrorBanner error={error} onRetry={reload} />}
      {!loading && !error && plugins.length === 0 && <p className="detail__muted">—</p>}
      {groups.map(([type, list]) => (
        <div key={type} className="settings__group">
          <h3 className="settings__subhead">{t(`plugins.type.${type}`)}</h3>
          <ul className="plugins">
            {list.map((p) => (
              <PluginItem key={p.namespace} plugin={p} onSaved={reload} />
            ))}
          </ul>
        </div>
      ))}
    </>
  );
}

function PluginItem({ plugin, onSaved }: { plugin: PluginInfo; onSaved: () => void }) {
  const hasConfig = (plugin.parameters?.length ?? 0) > 0;
  const [open, setOpen] = useState(false);
  return (
    <li className="plugins__item">
      <div className="plugins__head">
        <strong>{plugin.name}</strong>
        {plugin.version && <span className="plugins__ver">v{plugin.version}</span>}
        {plugin.login_from && (
          <span className="plugins__ver" title={`Uses login: ${plugin.login_from}`}>
            🔗 {plugin.login_from}
          </span>
        )}
        {hasConfig && (
          <button
            type="button"
            className={`btn btn--small plugins__configure ${open ? 'btn--active' : ''}`}
            aria-expanded={open}
            onClick={() => setOpen((v) => !v)}
          >
            {t('plugins.configure')}
          </button>
        )}
      </div>
      {plugin.description && <div className="plugins__desc">{plugin.description}</div>}
      {hasConfig && open && <PluginConfigForm plugin={plugin} onSaved={onSaved} />}
    </li>
  );
}

function PluginConfigForm({ plugin, onSaved }: { plugin: PluginInfo; onSaved: () => void }) {
  const params = useMemo(() => plugin.parameters ?? [], [plugin.parameters]);
  const initial = useCallback(() => {
    const out: Record<string, string> = {};
    for (const param of params) {
      const stored = plugin.config?.[param.name];
      out[param.name] = stored ?? (param.default != null ? String(param.default) : '');
    }
    return out;
  }, [params, plugin.config]);

  const [values, setValues] = useState<Record<string, string>>(initial);
  const [busy, setBusy] = useState(false);

  // Re-sync when the plugin list reloads (e.g. after a save).
  useEffect(() => setValues(initial()), [initial]);

  const onSubmit = useCallback(
    async (e: FormEvent) => {
      e.preventDefault();
      setBusy(true);
      try {
        // Unchanged secrets are still the mask; the backend ignores those.
        await setPluginConfig(plugin.namespace, values);
        toast.success(t('settings.saved'));
        onSaved();
      } catch (err) {
        toast.error(err instanceof Error ? err.message : String(err));
      } finally {
        setBusy(false);
      }
    },
    [plugin.namespace, values, onSaved],
  );

  return (
    <form className="settings__form plugins__config" onSubmit={onSubmit}>
      {params.map((param) => {
        const isSecret = param.secret || param.type === 'password';
        const isBool = param.type === 'bool';
        if (isBool) {
          const checked = ['1', 'true', 'yes', 'on'].includes(
            (values[param.name] ?? '').toLowerCase(),
          );
          return (
            <label key={param.name} className="plugins__field plugins__field--bool">
              <span className="plugins__fieldlabel">{param.name}</span>
              <label className="switch">
                <input
                  type="checkbox"
                  checked={checked}
                  onChange={(e) =>
                    setValues((v) => ({
                      ...v,
                      [param.name]: e.target.checked ? 'true' : 'false',
                    }))
                  }
                />
                <span className="switch__track" />
              </label>
              {param.description && (
                <span className="plugins__hint">{param.description}</span>
              )}
            </label>
          );
        }
        return (
          <label key={param.name} className="plugins__field">
            <span className="plugins__fieldlabel">
              {param.name}
              {isSecret && <span className="plugins__secret"> (secret)</span>}
            </span>
            <input
              type={isSecret ? 'password' : 'text'}
              autoComplete={isSecret ? 'new-password' : 'off'}
              placeholder={param.description || param.name}
              value={values[param.name] ?? ''}
              onChange={(e) =>
                setValues((v) => ({ ...v, [param.name]: e.target.value }))
              }
            />
            {param.description && (
              <span className="plugins__hint">{param.description}</span>
            )}
          </label>
        );
      })}
      <div className="settings__actions">
        <button type="submit" className="btn btn--primary" disabled={busy}>
          {busy ? '…' : t('common.save')}
        </button>
      </div>
    </form>
  );
}
