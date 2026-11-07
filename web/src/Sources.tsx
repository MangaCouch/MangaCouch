import { Select } from "./Controls";
import { useFeedback } from "./feedback";
import { useEffect, useState } from "react";
import { Check, RefreshCw } from "lucide-react";
import { aborted, ApiError, errorMessage, post, put, request } from "./api";
import type { Translate } from "./types";

type Settings = {
  download_limit_bytes: number;
  comments: boolean;
  user_agent: string;
  auto_free: boolean;
  default_quality: "org" | "res";
};
type Config = {
  settings: Settings;
  configured: Record<string, boolean>;
  revision: number;
};
const changedFrom = (settings: Settings, saved: Settings) =>
  (Object.keys(settings) as (keyof Settings)[]).some(
    (key) => settings[key] !== saved[key],
  );

export function SourcesSettings({ t }: { t: Translate }) {
  const [config, setConfig] = useState<Config | null>(null),
    // Unsaved setting changes and typed secrets stay here until a save succeeds.
    [draft, setDraft] = useState<Partial<Settings>>({}),
    [secrets, setSecrets] = useState<Record<string, string>>({}),
    [busy, setBusy] = useState(false),
    [error, setError] = useState<unknown>(null),
    [notice, setNotice] = useState(""),
    [verified, setVerified] = useState<Record<string, boolean>>({}),
    [checking, setChecking] = useState<string | null>(null);
  useFeedback(error ? errorMessage(error, t) : "");
  useEffect(() => {
    const controller = new AbortController();
    void request<Config>("/api/sources/settings", { signal: controller.signal })
      .then(setConfig)
      .catch((error) => {
        if (!aborted(error)) setError(error);
      });
    return () => controller.abort();
  }, []);
  async function verify(site: string) {
    setChecking(site);
    setError(null);
    try {
      const result = await post<{
        reachable: boolean;
        authenticated: boolean;
        detail: string | null;
      }>(`/api/sources/verify/${site}`, {});
      if (!result.reachable || !result.authenticated)
        throw new ApiError(400, result.detail ?? "source_login_required");
      setVerified((old) => ({ ...old, [site]: true }));
    } catch (error) {
      setVerified((old) => ({ ...old, [site]: false }));
      setError(error);
    } finally {
      setChecking(null);
    }
  }
  /** Saves `change` plus any earlier unsaved change; does nothing when nothing differs. */
  async function save(
    change: Partial<Settings> = {},
    secretChanges: Record<string, string> = {},
  ) {
    if (!config || busy) return;
    const pending = { ...draft, ...change };
    const settings = { ...config.settings, ...pending };
    if (
      !changedFrom(settings, config.settings) &&
      !Object.keys(secretChanges).length
    ) {
      setDraft({});
      return;
    }
    setDraft(pending);
    setBusy(true);
    setError(null);
    setNotice("");
    try {
      const saved = await put<Config>("/api/sources/settings", {
        settings,
        secrets: secretChanges,
        expected_revision: config.revision,
      });
      setConfig(saved);
      setDraft({});
      setSecrets((old) => {
        const next = { ...old };
        for (const key of Object.keys(secretChanges)) delete next[key];
        return next;
      });
      setVerified({});
    } catch (error) {
      if (error instanceof ApiError && error.status === 409) {
        // Another tab or device saved first: show its values, keep this change for an explicit retry.
        try {
          setConfig(await request<Config>("/api/sources/settings"));
          setNotice(
            t(
              "来源设置已在别处修改并已重新载入；你的修改尚未保存。",
              "Source settings changed elsewhere and were reloaded. Your change is not saved yet.",
            ),
          );
        } catch (reload) {
          setError(reload);
        }
      } else setError(error);
    } finally {
      setBusy(false);
    }
  }
  const typedSecrets = Object.fromEntries(
    Object.entries(secrets).filter(([, value]) => value),
  );
  const unsaved =
    !!config &&
    (changedFrom({ ...config.settings, ...draft }, config.settings) ||
      Object.keys(typedSecrets).length > 0);
  const secret = (key: string, label: string) => (
    <label>
      {label}
      <div className="source-secret">
        <input
          type="password"
          autoComplete="new-password"
          value={secrets[key] ?? ""}
          disabled={busy}
          placeholder={
            config?.configured[key]
              ? t("已配置", "Configured")
              : t("未配置", "Not configured")
          }
          onChange={(event) =>
            setSecrets((old) => ({ ...old, [key]: event.target.value }))
          }
        />
        <button
          type="button"
          disabled={busy || !secrets[key]}
          aria-label={t(`保存 ${label}`, `Save ${label}`)}
          onClick={() => void save({}, { [key]: secrets[key]! })}
        >
          {t("保存", "Save")}
        </button>
        <button
          type="button"
          className="icon-button danger"
          disabled={busy || !config?.configured[key]}
          aria-label={t(`清除 ${label}`, `Clear ${label}`)}
          onClick={() => void save({}, { [key]: "" })}
        >
          ×
        </button>
      </div>
    </label>
  );
  const heading = (site: string, label: string) => (
    <h3>
      {label}
      {verified[site] && (
        <Check size={17} aria-label={t("验证成功", "Verified")} />
      )}
      <button
        className="icon-button"
        disabled={!!checking || busy}
        aria-label={t(`验证 ${label}`, `Verify ${label}`)}
        onClick={() => void verify(site)}
      >
        <RefreshCw size={15} className={checking === site ? "spinner" : ""} />
      </button>
    </h3>
  );
  if (!config)
    return error ? (
      <p className="error-text">{errorMessage(error, t)}</p>
    ) : (
      <p>{t("读取来源设置…", "Loading source settings…")}</p>
    );
  const values = { ...config.settings, ...draft };
  return (
    <section className="source-config">
      {heading("ehentai", "E-Hentai")}
      {secret("eh_cookie", "E-Hentai Cookie")}
      {heading("exhentai", "ExHentai")}
      {secret("ex_cookie", "ExHentai Cookie")}
      <label>
        {t("默认下载质量", "Default quality")}
        <Select
          disabled={busy}
          value={values.default_quality ?? "org"}
          onChange={(event) =>
            void save({
              default_quality: event.target
                .value as Settings["default_quality"],
            })
          }
        >
          <option value="org">Original</option>
          <option value="res">Resample</option>
        </Select>
      </label>
      {heading("nhentai", "nHentai")}
      {secret("nh_cookie", "nHentai Cookie")}
      {secret("nh_key", "nHentai API key")}
      <label>
        User-Agent
        <input
          value={values.user_agent}
          disabled={busy}
          onChange={(event) =>
            setDraft((old) => ({ ...old, user_agent: event.target.value }))
          }
          onBlur={() => void save()}
        />
      </label>
      <label className="checkbox">
        <input
          type="checkbox"
          checked={values.auto_free ?? false}
          disabled={busy}
          onChange={(event) => void save({ auto_free: event.target.checked })}
        />
        {t("自动下载免费本子", "Automatically download free galleries")}
      </label>
      <label className="checkbox">
        <input
          type="checkbox"
          checked={values.comments}
          disabled={busy}
          onChange={(event) => void save({ comments: event.target.checked })}
        />
        {t("获取来源评论", "Fetch source comments")}
      </label>
      {secret("proxy", t("代理地址", "Proxy URL"))}
      {notice && (
        <p className="notice" role="status">
          {notice}
        </p>
      )}
      {(notice || !!error) && unsaved && (
        <div className="actions">
          <button disabled={busy} onClick={() => void save({}, typedSecrets)}>
            {t("重新保存我的修改", "Save my change again")}
          </button>
        </div>
      )}
      <span className="muted small" role="status">
        {busy ? t("正在保存…", "Saving…") : ""}
      </span>
    </section>
  );
}
