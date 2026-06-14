// Settings / admin view (owner): read & update config, trigger scan & thumb
// regen, upload an archive, list plugins, and set client-side preferences
// (theme, auto-lock idle timeout, language).

import { useCallback, useEffect, useRef, useState } from 'react';
import {
  getConfig,
  listPlugins,
  regenThumbnails,
  scanLibrary,
  updateConfig,
  uploadArchive,
} from '../api/endpoints';
import type { AppConfig, PluginInfo } from '../api/types';
import { useAsync } from '../hooks/useApi';
import { useTheme } from '../hooks/useTheme';
import { AUTOLOCK_KEY } from '../hooks/useAuth';
import { lsGet, lsSet } from '../lib/storage';
import { Spinner, ErrorBanner } from '../components/ui';
import { t } from '../i18n/strings';

export function Settings() {
  const { theme, setTheme } = useTheme();
  const [autolock, setAutolock] = useState<number>(() => lsGet<number>(AUTOLOCK_KEY, 0));

  const onAutolock = useCallback((minutes: number) => {
    setAutolock(minutes);
    lsSet(AUTOLOCK_KEY, minutes);
  }, []);

  return (
    <div className="settings">
      <h1>{t('settings.title')}</h1>

      <section className="panel">
        <h2>Client preferences</h2>
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
      </section>

      <AdminActions />
      <UploadPanel />
      <ConfigPanel />
      <PluginsPanel />
    </div>
  );
}

function AdminActions() {
  const [msg, setMsg] = useState<string | null>(null);
  const [busy, setBusy] = useState<string | null>(null);

  const run = useCallback(async (label: string, fn: () => Promise<unknown>) => {
    setBusy(label);
    setMsg(null);
    try {
      await fn();
      setMsg(`${label}: started.`);
    } catch (err) {
      setMsg(`${label}: ${err instanceof Error ? err.message : String(err)}`);
    } finally {
      setBusy(null);
    }
  }, []);

  return (
    <section className="panel">
      <h2>Library</h2>
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
          onClick={() => run(t('settings.regen'), regenThumbnails)}
        >
          {busy === t('settings.regen') ? '…' : t('settings.regen')}
        </button>
      </div>
      {msg && <div className="downloads__msg">{msg}</div>}
    </section>
  );
}

function UploadPanel() {
  const [progress, setProgress] = useState<number | null>(null);
  const [msg, setMsg] = useState<string | null>(null);
  const inputRef = useRef<HTMLInputElement | null>(null);

  const onFile = useCallback(async (file: File) => {
    setProgress(0);
    setMsg(null);
    try {
      const res = await uploadArchive(file, (f) => setProgress(f));
      setProgress(1);
      setMsg(res.id ? `Uploaded (id ${res.id}).` : 'Uploaded.');
    } catch (err) {
      setMsg(err instanceof Error ? err.message : String(err));
    } finally {
      setTimeout(() => setProgress(null), 800);
      if (inputRef.current) inputRef.current.value = '';
    }
  }, []);

  return (
    <section className="panel">
      <h2>{t('settings.upload')}</h2>
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
      {msg && <div className="downloads__msg">{msg}</div>}
    </section>
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
      setSaveMsg('Saved.');
      reload();
    } catch (err) {
      setSaveMsg(err instanceof Error ? err.message : String(err));
    } finally {
      setSaving(false);
    }
  }, [text, reload]);

  return (
    <section className="panel">
      <h2>{t('settings.config')}</h2>
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
          {saveMsg && <div className="downloads__msg">{saveMsg}</div>}
        </>
      )}
    </section>
  );
}

function PluginsPanel() {
  const { data, error, loading, reload } = useAsync<{ plugins: PluginInfo[] }>(
    (signal) => listPlugins(signal),
    [],
  );
  const plugins = data?.plugins ?? [];
  return (
    <section className="panel">
      <h2>{t('settings.plugins')}</h2>
      {loading && <Spinner />}
      {error && <ErrorBanner error={error} onRetry={reload} />}
      {!loading && !error && plugins.length === 0 && <p className="detail__muted">—</p>}
      {plugins.length > 0 && (
        <ul className="plugins">
          {plugins.map((p) => (
            <li key={p.namespace} className="plugins__item">
              <div className="plugins__head">
                <strong>{p.name}</strong>
                <span className={`plugin-type plugin-type--${p.type}`}>{p.type}</span>
                {p.version && <span className="plugins__ver">v{p.version}</span>}
              </div>
              {p.description && <div className="plugins__desc">{p.description}</div>}
            </li>
          ))}
        </ul>
      )}
    </section>
  );
}
