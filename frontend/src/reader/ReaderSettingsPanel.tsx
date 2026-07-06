// Reader settings panel, grouped into Layout / Display / Playback sections.
// Changes persist via useReader.updateSettings (which saves to localStorage
// as the new defaults).

import type { ReactNode } from 'react';
import { useTheme } from '../hooks/useTheme';
import { t } from '../i18n/strings';
import type { FitMode } from './settings';
import type { ReaderState } from './useReader';

const FITS: FitMode[] = ['width', 'height', 'container', 'original'];

export function ReaderSettingsPanel({ reader }: { reader: ReaderState }) {
  const { settings, updateSettings } = reader;
  const { theme, toggle } = useTheme();

  return (
    <div className="reader-settings">
      <Group title={t('reader.group.layout')}>
        <Field label={t('reader.mode')}>
          <Segmented
            value={settings.mode}
            options={[
              ['paged', t('reader.mode.paged')],
              ['scroll', t('reader.mode.scroll')],
            ]}
            onChange={(v) => updateSettings({ mode: v as 'paged' | 'scroll' })}
          />
        </Field>

        {settings.mode === 'paged' && (
          <>
            <Field label={t('reader.direction')}>
              <Segmented
                value={settings.direction}
                options={[
                  ['ltr', 'LTR'],
                  ['rtl', 'RTL'],
                ]}
                onChange={(v) => updateSettings({ direction: v as 'ltr' | 'rtl' })}
              />
            </Field>

            <Field label={t('reader.double')}>
              <label className="switch">
                <input
                  type="checkbox"
                  checked={settings.doublePage}
                  onChange={(e) => updateSettings({ doublePage: e.target.checked })}
                />
                <span className="switch__track" />
              </label>
            </Field>
          </>
        )}
      </Group>

      <Group title={t('reader.group.display')}>
        <Field label={t('reader.fit')}>
          <select
            className="select"
            value={settings.fit}
            onChange={(e) => updateSettings({ fit: e.target.value as FitMode })}
          >
            {FITS.map((f) => (
              <option key={f} value={f}>
                {t(`reader.fit.${f}`)}
              </option>
            ))}
          </select>
        </Field>

        <Field label={t('reader.theme')}>
          <button type="button" className="btn btn--small" onClick={toggle}>
            {theme === 'dark' ? '☀ Light' : '☾ Dark'}
          </button>
        </Field>
      </Group>

      <Group title={t('reader.group.playback')}>
        <Field label={t('reader.preload')}>
          <input
            type="range"
            min={0}
            max={8}
            value={settings.preload}
            aria-label={t('reader.preload')}
            onChange={(e) => updateSettings({ preload: Number(e.target.value) })}
          />
          <span className="reader-settings__value">{settings.preload}</span>
        </Field>

        <Field label={t('reader.autoplay')}>
          <input
            type="range"
            min={0}
            max={30}
            value={settings.autoplaySeconds}
            aria-label={t('reader.autoplay')}
            onChange={(e) => updateSettings({ autoplaySeconds: Number(e.target.value) })}
          />
          <span className="reader-settings__value">
            {settings.autoplaySeconds === 0 ? 'off' : `${settings.autoplaySeconds}s`}
          </span>
        </Field>
      </Group>
    </div>
  );
}

function Group({ title, children }: { title: string; children: ReactNode }) {
  return (
    <section className="reader-settings__group">
      <h3 className="reader-settings__group-title">{title}</h3>
      {children}
    </section>
  );
}

function Field({ label, children }: { label: string; children: ReactNode }) {
  return (
    <div className="reader-settings__field">
      <span className="reader-settings__label">{label}</span>
      <div className="reader-settings__control">{children}</div>
    </div>
  );
}

function Segmented({
  value,
  options,
  onChange,
}: {
  value: string;
  options: [string, string][];
  onChange: (v: string) => void;
}) {
  return (
    <div className="segmented">
      {options.map(([val, label]) => (
        <button
          key={val}
          type="button"
          className={`segmented__btn ${value === val ? 'segmented__btn--active' : ''}`}
          onClick={() => onChange(val)}
        >
          {label}
        </button>
      ))}
    </div>
  );
}
