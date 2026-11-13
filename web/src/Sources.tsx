import { InfoTip } from "./InfoTip";
import type { GuardRef } from "./navigation";
import { Select, SkeletonRows } from "./Controls";
import { Modal } from "./Modal";
import { useCallback, useEffect, useRef, useState } from "react";
import { Check, RefreshCw, X } from "lucide-react";
import { aborted, ApiError, errorMessage, post, put, request } from "./api";
import { ErrorNotice } from "./feedback";
import { dateTime } from "./format";
import { usePolling } from "./polling";
import type { Translate } from "./types";

type Settings = {
  online_paused: boolean;
  download_limit_bytes: number;
  download_reserve_bytes: number;
  comments: boolean;
  background_comments: boolean;
  eh_budget_capacity: number;
  eh_budget_leak_per_min: number;
  eh_max_concurrency: number;
  user_agent: string;
  auto_free: boolean;
  default_quality: "org" | "res";
  allow_fake_ip: boolean;
};
type RateEvent = {
  at: number;
  kind: "api" | "page" | "search" | "archiver";
  reason: "ban" | "too_fast" | "http_429";
  until: number;
  /** Bucket level and remaining share (0..1) when the rate limit came. */
  level: number;
  remaining: number;
  /** The capacity and drain factor it left in force for a day. */
  penalty: number;
};
type Budget = {
  ehentai: {
    level: number;
    capacity: number;
    leak_per_min: number;
    remaining: number;
    /** Seconds until the bucket is empty again, with no further requests. */
    empty_in: number;
    penalty: number;
    penalty_until: number | null;
  };
};
type Usage = { budget: Budget; cooldowns: Record<string, number> };
type Config = Usage & {
  balance_gp?: number | null;
  settings: Settings;
  configured: Record<string, boolean>;
  revision: number;
  /** The stored credentials cannot be read with the key file: online sources are off. */
  key_lost: boolean;
  /** The latest rate limits E(x)Hentai answered with, newest first. */
  rate_events: RateEvent[];
};
/** E(x)Hentai usage bucket settings: the server's defaults and ranges. */
const LIMITS = {
  download_reserve_bytes: { initial: 10 * 1024 ** 3, min: 0, max: 1024 ** 5 },
  download_limit_bytes: { initial: 32 * 1024 ** 3, min: 1, max: 1024 ** 5 },
  eh_budget_capacity: { initial: 1100, min: 100, max: 3000 },
  eh_budget_leak_per_min: { initial: 13, min: 1, max: 60 },
  eh_max_concurrency: { initial: 4, min: 1, max: 8 },
} as const;
type LimitKey = keyof typeof LIMITS;
/** Both byte sizes are shown in GiB, the unit their server defaults (32 GiB, 10 GiB) are whole in. */
type SizeUnit = { bytes: number; name: string };
const GIB: SizeUnit = { bytes: 1024 ** 3, name: "GiB" };
const validLimit = (key: LimitKey, value: number) =>
  Number.isInteger(value) &&
  value >= LIMITS[key].min &&
  value <= LIMITS[key].max;
/** How often the settings page refreshes the budget while it is open. */
const USAGE_REFRESH_MS = 10_000;
const percent = (share: number) => `${Math.round(share * 100)}%`;
function duration(seconds: number, t: Translate) {
  const minutes = Math.ceil(seconds / 60);
  if (minutes < 60) return t(`${minutes} 分钟`, `${minutes} min`);
  const hours = Math.floor(minutes / 60),
    rest = minutes % 60;
  return rest
    ? t(`${hours} 小时 ${rest} 分钟`, `${hours} h ${rest} min`)
    : t(`${hours} 小时`, `${hours} h`);
}
function rateEventLine(event: RateEvent, t: Translate) {
  const locale = t("zh-CN", "en-US"),
    kind = {
      api: "API",
      page: t("页面", "page"),
      search: t("搜索", "search"),
      archiver: "archiver",
    }[event.kind],
    reason = {
      ban: t("IP 被封禁", "IP ban"),
      too_fast: t("“访问过快”警告", '"too fast" warning'),
      http_429: "HTTP 429",
    }[event.reason];
  return t(
    `${dateTime(event.at, locale)}：${reason}（${kind}请求，当时剩余 ${percent(event.remaining)}），暂停到 ${dateTime(event.until, locale)}`,
    `${dateTime(event.at, locale)}: ${reason} (${kind} request, ${percent(event.remaining)} left), halted until ${dateTime(event.until, locale)}`,
  );
}
function UsageMeter({ usage, t }: { usage: Usage; t: Translate }) {
  const locale = t("zh-CN", "en-US"),
    eh = usage.budget.ehentai,
    halted = usage.cooldowns.ehentai,
    // HH:MM, with the date when it is not today (a penalty lasts a day).
    clock = (at: number) =>
      dateTime(
        at,
        locale,
        new Date(at * 1000).toDateString() === new Date().toDateString()
          ? { timeStyle: "short" }
          : {
              month: "numeric",
              day: "numeric",
              hour: "numeric",
              minute: "2-digit",
            },
      );
  return (
    <div className="source-budget" role="status">
      <progress
        max={1}
        value={eh.remaining}
        aria-label={t("E-Hentai 剩余用量", "E-Hentai usage left")}
      />
      {(eh.empty_in > 0 || !!halted) && (
        <p className="small">
          {eh.empty_in > 0
            ? t(
                `EH 剩余 ${percent(eh.remaining)}，约 ${duration(eh.empty_in, t)}后回满`,
                `EH ${percent(eh.remaining)} left, full again in about ${duration(eh.empty_in, t)}`,
              )
            : ""}
          {halted && eh.empty_in > 0 ? " · " : ""}
          {halted &&
            t(`暂停到 ${clock(halted)}`, `Halted until ${clock(halted)}`)}
        </p>
      )}
      {eh.penalty_until && (
        <p className="small">
          {t(
            `降额中（容量和恢复速度 ×${eh.penalty}），到 ${clock(eh.penalty_until)}`,
            `Reduced after a rate limit (capacity and drain ×${eh.penalty}) until ${clock(eh.penalty_until)}`,
          )}
        </p>
      )}
    </div>
  );
}
const changedFrom = (settings: Settings, saved: Settings) =>
  (Object.keys(settings) as (keyof Settings)[]).some(
    (key) => settings[key] !== saved[key],
  );

export function SourcesSettings({
  t,
  guard,
}: {
  t: Translate;
  /** Holds navigation while a change is unsaved or still saving. */
  guard?: GuardRef;
}) {
  const [balance, setBalance] = useState<number | null>(null);
  const [limitText, setLimitText] = useState<Partial<Record<LimitKey, string>>>(
    {},
  );
  const credentialField = useRef<string | null>(null);
  const [config, setConfig] = useState<Config | null>(null),
    // Unsaved setting changes and typed secrets stay here until a save succeeds.
    [draft, setDraft] = useState<Partial<Settings>>({}),
    [secrets, setSecrets] = useState<Record<string, string>>({}),
    [busy, setBusy] = useState(false),
    [error, setError] = useState<unknown>(null),
    [notice, setNotice] = useState(""),
    [verified, setVerified] = useState<Record<string, boolean>>({}),
    [checking, setChecking] = useState<string | null>(null),
    // The stored secret waiting for confirmation before it is cleared.
    [clearing, setClearing] = useState<{ key: string; label: string } | null>(
      null,
    ),
    [resetting, setResetting] = useState(false),
    [loading, setLoading] = useState(true);
  const root = useRef<HTMLElement>(null);
  const latest = useRef({ config, draft, secrets });
  latest.current = { config, draft, secrets };
  const queue = useRef(Promise.resolve());
  const queued = useRef(0);
  function enqueue(work: () => Promise<void>) {
    queued.current++;
    setBusy(true);
    const job = queue.current
      .then(work)
      .catch(setError)
      .finally(() => {
        queued.current--;
        if (!queued.current) setBusy(false);
      });
    queue.current = job;
    return job;
  }
  function acceptConfig(value: Config) {
    latest.current.config = value;
    setConfig(value);
    setBalance(value.balance_gp ?? null);
  }
  // Only the latest load applies; unsaved changes live in `draft` and `secrets`, never in config.
  const loader = useRef<AbortController | null>(null);
  const load = useCallback(async () => {
    loader.current?.abort();
    const controller = new AbortController();
    loader.current = controller;
    setLoading(true);
    setError(null);
    try {
      const loaded = await request<Config>("/api/sources/settings", {
        signal: controller.signal,
      });
      if (!controller.signal.aborted) acceptConfig(loaded);
    } catch (error) {
      if (!aborted(error) && !controller.signal.aborted) setError(error);
    } finally {
      if (!controller.signal.aborted) setLoading(false);
    }
  }, []);
  useEffect(() => {
    void load();
    return () => loader.current?.abort();
  }, [load]);
  // The budget moves with every request; a failed refresh keeps the last known state.
  usePolling(
    async (signal) => {
      const usage = await request<Usage>("/api/sources/budget", { signal });
      if (!signal.aborted) setConfig((old) => old && { ...old, ...usage });
    },
    USAGE_REFRESH_MS,
    { enabled: !!config },
  );
  async function resetCredentialsNow() {
    setResetting(false);
    setError(null);
    setNotice("");
    try {
      acceptConfig(await post<Config>("/api/sources/reset-credentials", {}));
      setSecrets({});
      setVerified({});
      setNotice(
        t(
          "来源凭据已重置，在线请求保持暂停。请重新填写 Cookie 和代理，再恢复在线来源。",
          "Source credentials were reset. Requests stay paused: re-enter cookies and proxy settings, then resume online sources.",
        ),
      );
    } catch (error) {
      setError(error);
    }
  }
  /** Checks the values as typed (unsaved, never stored); empty fields use the saved ones. */
  async function verifyNow(site: string) {
    setChecking(site);
    setError(null);
    try {
      const result = await post<{
        reachable: boolean;
        authenticated: boolean;
        detail: string | null;
      }>(`/api/sources/verify/${site}`, {
        secrets: Object.fromEntries(
          Object.entries(latest.current.secrets).filter(([, value]) => value),
        ),
      });
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
  async function saveNow(
    change: Partial<Settings> = {},
    secretChanges: Record<string, string> = {},
  ) {
    const { config, draft } = latest.current;
    if (!config) return;
    credentialField.current = Object.keys(secretChanges)[0] ?? null;
    const pending = { ...draft, ...change };
    // A limit still out of range stays in its field (and unsaved) instead of failing the
    // other changes saved meanwhile.
    const outOfRange: Partial<Settings> = {};
    for (const key of Object.keys(LIMITS) as LimitKey[]) {
      const value = pending[key];
      if (value !== undefined && !validLimit(key, value)) {
        outOfRange[key] = value;
        delete pending[key];
      }
    }
    const settings = { ...config.settings, ...pending };
    if (
      !changedFrom(settings, config.settings) &&
      !Object.keys(secretChanges).length
    ) {
      return;
    }

    setError(null);
    setNotice("");
    try {
      const saved = await put<Config>(
        `/api/sources/settings?expected_revision=${config.revision}`,
        { settings, secrets: secretChanges },
      );
      acceptConfig(saved);
      setDraft((old) => {
        const next = { ...old };
        for (const key of Object.keys(pending) as (keyof Settings)[])
          if (next[key] === pending[key]) delete next[key];
        return next;
      });
      setSecrets((old) => {
        const next = { ...old };
        for (const key of Object.keys(secretChanges))
          if (next[key] === secretChanges[key]) delete next[key];
        return next;
      });
      setVerified({});
    } catch (error) {
      if (error instanceof ApiError && error.status === 409) {
        // Another tab or device saved first: show its values, keep this change for an explicit retry.
        try {
          acceptConfig(await request<Config>("/api/sources/settings"));
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
    }
  }
  const save = (
    change: Partial<Settings> = {},
    secretChanges: Record<string, string> = {},
  ) => {
    latest.current.draft = { ...latest.current.draft, ...change };
    setDraft(latest.current.draft);
    return enqueue(() => saveNow(change, secretChanges));
  };
  const verify = (site: string) => enqueue(() => verifyNow(site));
  const resetCredentials = () => enqueue(resetCredentialsNow);
  const typedSecrets = Object.fromEntries(
    Object.entries(secrets).filter(([, value]) => value),
  );
  const unsaved =
    !!config &&
    (changedFrom({ ...config.settings, ...draft }, config.settings) ||
      Object.keys(typedSecrets).length > 0);
  const held = unsaved || busy;
  useEffect(() => {
    if (guard)
      guard.current = async () => {
        if (!held) return true;
        const section = root.current?.closest("details");
        if (section) section.open = true;
        root.current?.scrollIntoView?.({ block: "start", behavior: "smooth" });
        return t(
          "来源设置尚未保存，请保存或放弃修改。",
          "Source settings are unsaved. Save or discard your changes.",
        );
      };
    const warn = (event: BeforeUnloadEvent) => {
      if (held) {
        event.preventDefault();
        event.returnValue = "";
      }
    };
    window.addEventListener("beforeunload", warn);
    return () => {
      if (guard) guard.current = null;
      window.removeEventListener("beforeunload", warn);
    };
  }, [guard, held, t]);
  function discard() {
    setLimitText({});
    setDraft({});
    setSecrets({});
    setNotice("");
    setError(null);
  }
  const secret = (key: string, label: string) => (
    <label>
      <span className="heading-with-info">
        {label}
        {key === "proxy" && (
          <InfoTip label={t("关于代理", "About proxy")}>
            {t(
              "来源请求不读取系统或环境变量中的代理；socks5h:// 由代理解析域名。",
              "Source requests ignore system and environment proxies; socks5h:// lets the proxy resolve host names.",
            )}
          </InfoTip>
        )}
      </span>
      <div className="source-secret">
        <input
          // Shown as typed so a pasted value can be checked; saved values are never sent back.
          type="text"
          autoComplete="off"
          spellCheck={false}
          value={secrets[key] ?? ""}

          // The server never sends a saved value back; the field only says one is saved.
          placeholder={
            config?.configured[key]
              ? t(
                  "已保存，输入新值以替换",
                  "Saved — enter a new value to replace",
                )
              : key === "proxy"
                ? "socks5h://host:port"
                : t("未配置", "Not configured")
          }
          onChange={(event) => {
            setSecrets((old) => ({ ...old, [key]: event.target.value }));
            setVerified({});
          }}
        />
        <button
          type="button"
          disabled={!secrets[key]?.trim()}
          aria-label={t(`保存 ${label}`, `Save ${label}`)}
          onClick={() => void save({}, { [key]: secrets[key]! })}
        >
          {t("保存", "Save")}
        </button>
        <button
          type="button"
          className="icon-button danger"
          disabled={!config?.configured[key]}
          aria-label={t(`清除 ${label}`, `Clear ${label}`)}
          onClick={() => setClearing({ key, label })}
        >
          <X size={16} />
        </button>
      </div>
      {credentialField.current === key &&
        error instanceof ApiError &&
        ["invalid_proxy", "invalid_credential"].includes(error.code ?? "") && (
          <p role="alert" className="small error-text">
            {errorMessage(error, t)}
          </p>
        )}
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
        disabled={!!checking}
        aria-label={t(`验证 ${label}`, `Verify ${label}`)}
        onClick={() => void verify(site)}
      >
        <RefreshCw size={15} className={checking === site ? "spinner" : ""} />
      </button>
    </h3>
  );
  const failure = <ErrorNotice error={error} t={t} />;
  if (!config)
    return loading ? (
      <SkeletonRows label={t("读取来源设置…", "Loading source settings…")} />
    ) : (
      <ErrorNotice error={error} t={t}>
        <button type="button" onClick={() => void load()}>
          {t("重试", "Retry")}
        </button>
      </ErrorNotice>
    );
  const values = { ...config.settings, ...draft };
  const limit = (key: LimitKey, label: string, size?: SizeUnit) => {
    const unit = size?.bytes ?? 1;
    const value = values[key] ?? LIMITS[key].initial;
    const invalid = limitText[key] !== "" && !validLimit(key, value);
    const clearText = () => setLimitText(({ [key]: _, ...rest }) => rest);
    const { min, max } = LIMITS[key];
    // Bounds in the field's unit, rounded inward to what can be typed: a whole-unit maximum,
    // and a minimum below one unit (the download limit's 1 byte) reads as "more than 0".
    const upper = Math.floor(max / unit);
    const hint = !size
      ? t(
          `请输入 ${min}–${max} 之间的整数。`,
          `Enter a whole number from ${min}–${max}.`,
        )
      : min > 0 && min < unit
        ? t(
            `请输入大于 0、不超过 ${upper} ${size.name} 的数值。`,
            `Enter more than 0 and at most ${upper} ${size.name}.`,
          )
        : t(
            `请输入 ${min / unit}–${upper} ${size.name}。`,
            `Enter ${min / unit}–${upper} ${size.name}.`,
          );
    return (
      <label>
        {label}
        <input
          type="number"
          inputMode={unit === 1 ? "numeric" : "decimal"}
          min={min / unit}
          max={max / unit}
          step={unit === 1 ? 1 : "any"}
          value={limitText[key] ?? Number((value / unit).toFixed(6))}
          aria-invalid={invalid}
          onChange={(event) => {
            const text = event.target.value;
            setLimitText((old) => ({ ...old, [key]: text }));
            setDraft((old) => {
              const next = { ...old };
              if (!text.trim()) delete next[key];
              else
                next[key] =
                  unit === 1 ? Number(text) : Math.round(Number(text) * unit);
              return next;
            });
          }}
          onBlur={() => {
            if (limitText[key] === "") {
              clearText();
              return;
            }
            if (!invalid) {
              clearText();
              void save();
            }
          }}
        />
        {invalid && (
          <small role="alert" className="error-text">
            {hint}
          </small>
        )}
      </label>
    );
  };
  return (
    <section className="source-config" ref={root}>
      {values.online_paused && (
        <p className="notice" role="status">
          {t(
            "在线来源保持暂停。请核对账号、代理与当前站点暂停情况后恢复。",
            "Online sources remain paused. Check your account, proxy and current site halts before resuming.",
          )}
          <button onClick={() => void save({ online_paused: false })}>
            {t("恢复在线来源", "Resume online sources")}
          </button>
        </p>
      )}
      <div className="source-balance">
        <span>{t("GP 余额", "GP balance")}</span>
        <strong role="status">
          {balance === null
            ? "—"
            : new Intl.NumberFormat(t("zh-CN", "en-US")).format(balance)}
        </strong>
        <button
          type="button"
          className="icon-button"
          disabled={busy || config.key_lost}
          aria-label={t("刷新 GP 余额", "Refresh GP balance")}
          onClick={() =>
            void enqueue(async () => {
              const result = await post<{
                balance_gp: number | null;
                no_gallery?: boolean;
                probing?: boolean;
              }>("/api/sources/balance", {});
              setBalance(result.balance_gp);
              setNotice(
                result.no_gallery
                  ? t(
                      "请先添加一个画廊下载任务，再查询余额。",
                      "Add a gallery download before checking the balance.",
                    )
                  : result.probing
                    ? t(
                        "正在检查余额，请稍后刷新。",
                        "A balance check is in progress. Refresh shortly.",
                      )
                    : t("余额已更新。", "Balance updated."),
              );
            })
          }
        >
          <RefreshCw size={16} aria-hidden="true" />
        </button>
      </div>
      {limit(
        "download_limit_bytes",
        t("单个下载大小上限（GiB）", "Maximum download size (GiB)"),
        GIB,
      )}
      {limit(
        "download_reserve_bytes",
        t("下载磁盘保留空间（GiB）", "Download disk reserve (GiB)"),
        GIB,
      )}
      <small>
        {t(
          "低于保留空间时下载队列自动等待，每 5 分钟检查恢复。",
          "Downloads wait below this reserve and check every 5 minutes to resume.",
        )}
      </small>
      {config.key_lost && (
        <div className="notice error" role="alert">
          <span>
            {t(
              "来源凭据的密钥文件（数据库旁的 .sources.key）丢失或与数据库不匹配：已保存的 Cookie 和代理无法读取，在线来源已停用，下载和来源检查暂停。若找不回原来的密钥文件，可以重置：清除已保存的凭据并生成新密钥。设置和本地书籍保留，持有远端下载链接的下载会暂停。",
              "The source credential key file (.sources.key beside the database) is missing or does not match the database: the stored cookies and proxy cannot be read, so online sources, downloads and source checks are off. If the original key file cannot be restored, reset: the stored credentials are discarded and a new key is made. Settings and local books stay; downloads holding a remote link pause.",
            )}
          </span>
          <button
            type="button"
            className="danger"

            onClick={() => setResetting(true)}
          >
            {t("重置来源凭据", "Reset source credentials")}
          </button>
        </div>
      )}
      {failure}
      {heading("ehentai", "E-Hentai")}
      {secret("eh_cookie", "E-Hentai Cookie")}
      {heading("exhentai", "ExHentai")}
      {secret("ex_cookie", "ExHentai Cookie")}
      <label>
        {t("默认下载质量", "Default quality")}
        <Select
          value={values.default_quality ?? "org"}
          onChange={(event) =>
            void save({
              default_quality: event.target
                .value as Settings["default_quality"],
            })
          }
        >
          <option value="org">{t("原图", "Original")}</option>
          <option value="res">{t("重采样", "Resample")}</option>
        </Select>
      </label>
      <fieldset className="source-usage">
        <legend>
          <span className="heading-with-info">
            {t("E-Hentai 用量", "E-Hentai usage")}
            <InfoTip label={t("关于 E-Hentai 用量", "About E-Hentai usage")}>
              {" "}
              {t(
                "E-Hentai 和 ExHentai 按 IP 共用一个用量桶：画廊页 1 点，API 1.5 点，搜索 4.5 点，archiver 0.5 点。剩余不到一半时自动降低并发、拉开间隔；后台检查最多用到 80%。同一 IP 上用浏览器浏览 EH 也会占用额度。",
                "E-Hentai and ExHentai share one usage bucket per IP: a gallery page costs 1 point, an API call 1.5, a search 4.5 and archiver.php 0.5. Below half left, requests slow down and run fewer at a time; background checks use at most 80%. Browsing EH from the same IP spends from the same bucket.",
              )}{" "}
            </InfoTip>
          </span>
        </legend>
        <UsageMeter usage={config} t={t} />
        <div className="source-limits">
          {limit("eh_budget_capacity", t("容量（点）", "Capacity (points)"))}
          {limit(
            "eh_budget_leak_per_min",
            t("每分钟恢复（点）", "Recovery per minute (points)"),
          )}
          {limit("eh_max_concurrency", t("最大并发", "Max concurrency"))}
        </div>
        {config.rate_events.length > 0 && (
          <div className="source-rate-events">
            <span className="small">
              {t("最近被限速", "Recent rate limits")}
            </span>
            <ul className="muted small">
              {config.rate_events.map((event) => (
                <li key={`${event.at}-${event.kind}-${event.reason}`}>
                  {rateEventLine(event, t)}
                </li>
              ))}
            </ul>
          </div>
        )}
      </fieldset>
      <label className="checkbox">
        <input
          type="checkbox"
          checked={values.auto_free ?? false}

          onChange={(event) => void save({ auto_free: event.target.checked })}
        />
        {t("自动下载免费本子", "Automatically download free galleries")}
      </label>
      <label className="checkbox">
        <input
          type="checkbox"
          checked={values.comments}

          onChange={(event) => void save({ comments: event.target.checked })}
        />
        {t("获取来源评论", "Fetch source comments")}
      </label>
      <div className="heading-with-info">
        <label className="checkbox">
          <input
            type="checkbox"
            checked={values.comments && (values.background_comments ?? false)}
            disabled={!values.comments}
            onChange={(event) =>
              void save({ background_comments: event.target.checked })
            }
          />
          {t("后台检查也获取评论", "Fetch comments in background checks")}
        </label>
        <InfoTip label={t("关于后台评论", "About background comments")}>
          {t("每本书多一次请求。", "One extra request per book.")}
        </InfoTip>
      </div>
      <details>
        <summary>{t("高级", "Advanced")}</summary>
        <div className="source-advanced">
          <label>
            User-Agent
            <input
              value={values.user_agent}

              onChange={(event) =>
                setDraft((old) => ({ ...old, user_agent: event.target.value }))
              }
              onBlur={() => void save()}
            />
          </label>
          {secret("proxy", t("代理地址", "Proxy URL"))}
          <div className="heading-with-info">
            <label className="checkbox">
              <input
                type="checkbox"
                checked={values.allow_fake_ip ?? false}

                onChange={(event) =>
                  void save({ allow_fake_ip: event.target.checked })
                }
              />
              {t(
                "允许 fake-IP DNS（198.18.0.0/15）",
                "Allow fake-IP DNS answers (198.18.0.0/15)",
              )}
            </label>
            <InfoTip label={t("关于 fake-IP DNS", "About fake-IP DNS")}>
              {t(
                "仅在未填写代理、且 Clash/Surge 等 TUN 代理以 fake-IP 模式接管 DNS 时开启：来源域名解析到 198.18.0.0/15 时仍允许连接。其他内网地址和直接写 IP 的链接仍会被拒绝。",
                "Only for a TUN proxy such as Clash or Surge in fake-IP mode, with no proxy URL set: source host names that resolve into 198.18.0.0/15 may still connect. Other private addresses and IP-address links stay refused.",
              )}
            </InfoTip>
          </div>
        </div>
      </details>
      {notice && (
        <p className="notice" role="status">
          {notice}
        </p>
      )}
      {unsaved && !busy && (
        <div className="actions">
          <span className="small" role="status">
            {t(
              "有未保存的来源设置，离开本页前请保存或放弃。",
              "Source settings have unsaved changes. Save or discard them before leaving.",
            )}
          </span>
          {(notice || !!error) && (
            <button onClick={() => void save({}, typedSecrets)}>
              {t("重新保存我的修改", "Save my change again")}
            </button>
          )}
          <button type="button" onClick={discard}>
            {t("放弃未保存的修改", "Discard unsaved changes")}
          </button>
        </div>
      )}
      <span className="muted small" role="status">
        {busy ? t("正在保存…", "Saving…") : ""}
      </span>
      {resetting && (
        <Modal
          title={t("重置来源凭据？", "Reset source credentials?")}
          closeLabel={t("取消", "Cancel")}
          close={() => setResetting(false)}
        >
          <p>
            {t(
              "所有已保存的 Cookie 和代理都会被清除且无法恢复，之后需要重新填写。如果原来的密钥文件还能找回，请先停止服务并放回该文件，而不是重置。",
              "Every stored cookie and proxy is discarded for good; you will need to enter them again. If the original key file can still be found, stop the server and put it back instead.",
            )}
          </p>
          <div className="actions modal-actions">
            <button type="button" onClick={() => setResetting(false)}>
              {t("取消", "Cancel")}
            </button>
            <button
              type="button"
              className="danger"
              onClick={() => void resetCredentials()}
            >
              {t("重置", "Reset")}
            </button>
          </div>
        </Modal>
      )}
      {clearing && (
        <Modal
          title={t(`清除 ${clearing.label}？`, `Clear ${clearing.label}?`)}
          closeLabel={t("取消", "Cancel")}
          close={() => setClearing(null)}
        >
          <p>
            {t(
              "服务器上保存的值将被删除，之后需要重新填写。",
              "The value stored on the server is deleted; you will need to enter it again.",
            )}
          </p>
          <div className="actions modal-actions">
            <button type="button" onClick={() => setClearing(null)}>
              {t("取消", "Cancel")}
            </button>
            <button
              type="button"
              className="danger"
              onClick={() => {
                const { key } = clearing;
                setClearing(null);
                void save({}, { [key]: "" });
              }}
            >
              {t("清除", "Clear")}
            </button>
          </div>
        </Modal>
      )}
    </section>
  );
}
