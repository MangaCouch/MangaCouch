// Settings / admin view (owner): read & update config, trigger scan & thumb
// regen, upload an archive, list plugins, and set client-side preferences
// (theme, auto-lock idle timeout, language).

import { useCallback, useEffect, useMemo, useRef, useState, type FormEvent } from 'react';
import {
  changePasscode,
  getConfig,
  listPlugins,
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

      <SecurityPanel />
      <AdminActions />
      <UploadPanel />
      <ConfigPanel />
      <PluginsPanel />
    </div>
  );
}

function SecurityPanel() {
  const [currentPass, setCurrentPass] = useState('');
  const [ownerNew, setOwnerNew] = useState('');
  const [ownerConfirm, setOwnerConfirm] = useState('');
  const [readerNew, setReaderNew] = useState('');
  const [readerConfirm, setReaderConfirm] = useState('');
  const [msg, setMsg] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const submit = useCallback(
    async (role: 'owner' | 'reader', next: string, confirm: string, current?: string) => {
      setMsg(null);
      if (next.length < 4) {
        setMsg('New passcode must be at least 4 characters.');
        return;
      }
      if (next !== confirm) {
        setMsg('The two new-passcode fields do not match.');
        return;
      }
      setBusy(true);
      try {
        await changePasscode(role, next, current);
        setMsg(`${role} passcode changed.`);
        setCurrentPass('');
        setOwnerNew('');
        setOwnerConfirm('');
        setReaderNew('');
        setReaderConfirm('');
      } catch (err) {
        setMsg(err instanceof Error ? err.message : String(err));
      } finally {
        setBusy(false);
      }
    },
    [],
  );

  return (
    <section className="panel">
      <h2>Security</h2>

      <h3 className="settings__subhead">Change owner passcode</h3>
      <form
        className="settings__form"
        onSubmit={(e) => {
          e.preventDefault();
          submit('owner', ownerNew, ownerConfirm, currentPass);
        }}
      >
        <input
          type="password"
          autoComplete="current-password"
          placeholder="Current owner passcode"
          value={currentPass}
          onChange={(e) => setCurrentPass(e.target.value)}
        />
        <input
          type="password"
          autoComplete="new-password"
          placeholder="New owner passcode"
          value={ownerNew}
          onChange={(e) => setOwnerNew(e.target.value)}
        />
        <input
          type="password"
          autoComplete="new-password"
          placeholder="Confirm new passcode"
          value={ownerConfirm}
          onChange={(e) => setOwnerConfirm(e.target.value)}
        />
        <button type="submit" className="btn btn--primary" disabled={busy}>
          {busy ? '…' : 'Update owner passcode'}
        </button>
      </form>

      <h3 className="settings__subhead">Change reader passcode (shared, read-only)</h3>
      <form
        className="settings__form"
        onSubmit={(e) => {
          e.preventDefault();
          submit('reader', readerNew, readerConfirm);
        }}
      >
        <input
          type="password"
          autoComplete="new-password"
          placeholder="New reader passcode"
          value={readerNew}
          onChange={(e) => setReaderNew(e.target.value)}
        />
        <input
          type="password"
          autoComplete="new-password"
          placeholder="Confirm new passcode"
          value={readerConfirm}
          onChange={(e) => setReaderConfirm(e.target.value)}
        />
        <button type="submit" className="btn" disabled={busy}>
          {busy ? '…' : 'Update reader passcode'}
        </button>
      </form>

      {msg && <div className="downloads__msg">{msg}</div>}
    </section>
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
              {p.login_from && (
                <div className="plugins__desc">uses login: {p.login_from}</div>
              )}
              {p.parameters && p.parameters.length > 0 && (
                <PluginConfigForm plugin={p} onSaved={reload} />
              )}
            </li>
          ))}
        </ul>
      )}
    </section>
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
  const [msg, setMsg] = useState<string | null>(null);

  // Re-sync when the plugin list reloads (e.g. after a save).
  useEffect(() => setValues(initial()), [initial]);

  const onSubmit = useCallback(
    async (e: FormEvent) => {
      e.preventDefault();
      setBusy(true);
      setMsg(null);
      try {
        // Unchanged secrets are still the mask; the backend ignores those.
        await setPluginConfig(plugin.namespace, values);
        setMsg('Saved.');
        onSaved();
      } catch (err) {
        setMsg(err instanceof Error ? err.message : String(err));
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
          {busy ? '…' : 'Save'}
        </button>
        {msg && <span className="downloads__msg">{msg}</span>}
      </div>
    </form>
  );
}
