import { useViewState, useViewScroll } from "./viewState";
import { InfoTip } from "./InfoTip";
import { BackButton } from "./navigation";
import type { Guard, GuardRef } from "./navigation";
import { useEffect, useRef, useState, type ReactNode } from "react";
import { ChevronDown, LoaderCircle } from "lucide-react";
import { Select } from "./Controls";
import { Preferences } from "./PreferenceSettings";
import { Scan } from "./Scan";
import {
  SourceReviewList,
  DuplicateSettings,
  TranslationSettings,
} from "./LibrarySettings";
import { MetadataSync } from "./MetadataSync";
import { CacheSettings, cacheSummary, type CacheStatus } from "./Cache";
import { SourcesSettings } from "./Sources";
import { Devices } from "./Devices";
import { Credentials } from "./Credentials";
import { PasscodeChange } from "./Passcode";
import { Maintenance } from "./Maintenance";
import { RecycleBin } from "./RemovalDialog";
import { errorMessage, post, request } from "./api";
import { ErrorNotice } from "./feedback";
import { Pill } from "./Pills";
import { PollingVisible, usePolling } from "./polling";
import type { Lang, Translate } from "./types";
type Summary = {
  version: string;
  review: number;
  sync: Record<string, number>;
  indexed_pages: number;
  total_pages: number;
  translations: number;
  devices: number;
  recycle: number;
  cache?: CacheStatus;
};
type Diagnostics = {
  ready: boolean;
  library_root: string;
  database: string;
  books: number;
};
const RESTORE_COMMAND = `mangacouch restore /path/backup.zip /path/restored-state
mangacouch --db /path/restored-state/library.sqlite serve --origin <URL>`;

function Category({ title, children }: { title: string; children: ReactNode }) {
  return (
    <section className="settings-category panel">
      <h2>{title}</h2>
      <div className="settings-category-body">{children}</div>
    </section>
  );
}
/** A collapsible section: mounted on first open, kept mounted, but its polling pauses while collapsed. */
function SettingDetails({
  id,
  title,
  summary,
  info,
  children,
  reveal = false,
}: {
  id: string;
  title: string;
  info?: ReactNode;
  summary?: string | false | null;
  children: ReactNode;
  reveal?: boolean;
}) {
  const element = useRef<HTMLDetailsElement>(null);
  const [open, setOpen] = useViewState(`settings:${id}:open`, false),
    [visited, setVisited] = useState(open || reveal);
  useEffect(() => {
    if (reveal && element.current) {
      element.current.open = true;
      setOpen(true);
      setVisited(true);
      element.current.scrollIntoView?.({ block: "start" });
    }
  }, [reveal, setOpen]);
  return (
    <details
      ref={element}
      open={open}
      className="settings-section"
      onToggle={(event) => {
        const value = event.currentTarget.open;
        setOpen(value);
        if (value) setVisited(true);
      }}
    >
      <summary>
        <span>
          <span className="heading-with-info">
            <h3>{title}</h3>
            {info}
          </span>
          {summary && <small className="muted">{summary}</small>}
        </span>
        <ChevronDown size={18} />
      </summary>
      {visited && (
        <PollingVisible.Provider value={open}>
          <div className="settings-section-body">{children}</div>
        </PollingVisible.Provider>
      )}
    </details>
  );
}
/**
 * Downloads a backup: the server builds it first (so a failure shows here), then the browser
 * saves the finished file straight to disk with its own download.
 */
function BackupDownload({ t }: { t: Translate }) {
  const [preparing, setPreparing] = useState(false);
  const [error, setError] = useState<unknown>(null);
  const mounted = useRef(true);
  useEffect(
    () => () => {
      mounted.current = false;
    },
    [],
  );
  async function save() {
    if (preparing) return;
    setPreparing(true);
    setError(null);
    try {
      const { id } = await post<{ id: string }>("/api/snapshots");
      const link = document.createElement("a");
      link.href = `/api/snapshots/${encodeURIComponent(id)}`;
      link.download = "mangacouch-snapshot.zip";
      document.body.append(link);
      link.click();
      link.remove();
    } catch (failure) {
      if (mounted.current) setError(failure);
    } finally {
      if (mounted.current) setPreparing(false);
    }
  }
  return (
    <>
      <button
        type="button"
        className="button"
        disabled={preparing}
        onClick={() => void save()}
      >
        {preparing && <LoaderCircle size={16} className="spinner" />}
        {t("下载书库备份", "Download library backup")}
      </button>
      {preparing && (
        <p className="muted small" role="status">
          {t("正在准备…", "Preparing…")}
        </p>
      )}
      <ErrorNotice error={error} t={t} />
    </>
  );
}
function Storage({ t }: { t: Translate }) {
  const [data, setData] = useState<Diagnostics | null>(null);
  const { error } = usePolling(async (signal) => {
    const value = await request<Diagnostics>("/api/diagnostics", { signal });
    if (!signal.aborted) setData(value);
  }, 30_000);
  return (
    <section>
      <h3>{t("存储状态", "Storage status")}</h3>
      {!!error && (
        <p className="notice error" role="alert">
          {errorMessage(error, t)}
        </p>
      )}
      {data && (
        <>
          <Pill>
            {data.ready
              ? t("连接正常", "Connected")
              : t("书库路径不可用", "Library unavailable")}
          </Pill>
          <dl className="settings-facts">
            <dt>{t("书库路径", "Library path")}</dt>
            <dd>{data.library_root}</dd>
            <dt>{t("数据库", "Database")}</dt>
            <dd>{data.database}</dd>
            <dt>{t("书籍", "Books")}</dt>
            <dd>{data.books}</dd>
          </dl>
        </>
      )}
    </section>
  );
}
export function Settings({
  section,
  t,
  guard,
  navigate,
  lang,
  changeLanguage,
  logout,
  lock,
}: {
  section?: string;
  t: Translate;
  guard: GuardRef;
  navigate: (route: string) => void;
  lang: Lang;
  changeLanguage: (lang: Lang) => void;
  logout: () => void;
  lock: (confirmed: boolean) => void;
}) {
  const [summary, setSummary] = useState<Summary | null>(null);
  usePolling(async (signal) => {
    const value = await request<Summary>("/api/settings/summary", { signal });
    if (!signal.aborted) setSummary(value);
  }, 15_000);
  const preferenceGuard = useRef<Guard | null>(null),
    syncGuard = useRef<Guard | null>(null),
    credentialGuard = useRef<Guard | null>(null),
    sourceGuard = useRef<Guard | null>(null);
  useEffect(() => {
    guard.current = async () => {
      for (const part of [
        preferenceGuard,
        syncGuard,
        credentialGuard,
        sourceGuard,
      ]) {
        const allowed = part.current ? await part.current() : true;
        if (allowed !== true) return allowed;
      }
      return true;
    };
    return () => {
      guard.current = null;
    };
  }, [guard]);
  useViewScroll("settings", summary !== null);
  const sync = summary?.sync ?? {};
  return (
    <main className="content settings-page">
      <BackButton t={t} />
      <h1>{t("设置", "Settings")}</h1>
      <Category title={t("阅读与显示", "Reading & display")}>
        <label className="settings-field">
          {t("界面语言", "Interface language")}
          <Select
            value={lang}
            onChange={(event) => changeLanguage(event.target.value as Lang)}
          >
            <option value="zh">中文</option>
            <option value="en">English</option>
          </Select>
        </label>
        <Preferences t={t} guard={preferenceGuard} />
      </Category>
      <Category title={t("书库管理", "Library management")}>
        <SettingDetails
          id="scan"
          title={t("扫描书库", "Scan library")}
          reveal={section === "scan"}
        >
          <Scan t={t} navigate={navigate} />
        </SettingDetails>
        <SettingDetails
          id="duplicates"
          title={t("书籍去重", "Duplicate books")}
          reveal={section === "duplicates"}
          summary={
            summary &&
            t(
              `已分析 ${summary.indexed_pages}/${summary.total_pages} 页`,
              `${summary.indexed_pages}/${summary.total_pages} pages analyzed`,
            )
          }
        >
          <DuplicateSettings t={t} navigate={navigate} />
        </SettingDetails>
        <SettingDetails
          id="source-checks"
          title={t("来源核对", "Source review")}
          reveal={section === "source-checks"}
          summary={
            summary &&
            t(
              `${summary.review} 本需核对`,
              `${summary.review} books awaiting review`,
            )
          }
        >
          <SourceReviewList t={t} navigate={navigate} />
        </SettingDetails>
        <SettingDetails
          id="sync"
          title={t("元数据同步", "Metadata sync")}
          summary={
            summary &&
            t(
              `待同步 ${sync.pending ?? 0}，失败 ${sync.error ?? 0}，差异 ${sync.conflict ?? 0}`,
              `${sync.pending ?? 0} pending, ${sync.error ?? 0} failed, ${sync.conflict ?? 0} conflicts`,
            )
          }
        >
          <MetadataSync t={t} guard={syncGuard} navigate={navigate} />
        </SettingDetails>
        <SettingDetails
          id="translations"
          title={t("标签译名", "Tag translations")}
          summary={
            summary &&
            t(
              `${summary.translations} 条自定义译名`,
              `${summary.translations} custom translations`,
            )
          }
        >
          <TranslationSettings t={t} />
        </SettingDetails>
        <SettingDetails
          id="cache"
          title={t("缓存", "Cache")}
          info={
            <InfoTip label={t("关于缓存", "About cache")}>
              {t(
                "入库后会在后台先为每本书生成封面（第一页），再为所有页生成 WebP 缩略图，存入可丢弃的磁盘缓存。缓存将满时缩略图生成会自动停止，不挤掉已有的封面；缓存满了先淘汰最早的。",
                "After import, each book's cover (its first page) and then WebP thumbnails of all its pages are rendered in the background into a disposable disk cache. Thumbnail rendering stops when the cache is nearly full instead of pushing covers out; when full, the oldest entries go first.",
              )}
            </InfoTip>
          }
          reveal={section === "cache"}
          summary={summary?.cache && cacheSummary(summary.cache, t)}
        >
          <CacheSettings t={t} />
        </SettingDetails>
      </Category>
      <Category title={t("在线来源", "Online sources")}>
        <SettingDetails
          id="sources"
          title={t("来源账户与下载", "Source accounts & downloads")}
          reveal={section === "sources"}
        >
          <SourcesSettings t={t} guard={sourceGuard} />
        </SettingDetails>
      </Category>
      <Category title={t("访问与连接", "Access & connections")}>
        <SettingDetails
          id="devices"
          title={t("网页设备", "Web devices")}
          summary={
            summary &&
            t(
              `${summary.devices} 台已登录设备`,
              summary.devices === 1
                ? "1 signed-in device"
                : `${summary.devices} signed-in devices`,
            )
          }
        >
          <Devices t={t} logout={logout} />
        </SettingDetails>
        <SettingDetails id="keys" title={t("API 密钥", "API keys")}>
          <Credentials t={t} guard={credentialGuard} />
        </SettingDetails>
        <SettingDetails
          id="passcode"
          title={t("修改书库口令", "Change library passcode")}
        >
          <PasscodeChange t={t} lock={lock} />
        </SettingDetails>
        <SettingDetails
          id="opds"
          title={t("外部阅读器（OPDS）", "External readers (OPDS)")}
        >
          <label>
            OPDS URL
            <input
              readOnly
              value={`${location.origin}/opds`}
              onFocus={(event) => event.target.select()}
            />
          </label>
          <dl className="settings-facts">
            <dt>{t("用户名", "Username")}</dt>
            <dd>{t("任意", "Any")}</dd>
            <dt>{t("密码", "Password")}</dt>
            <dd>{t("API 密钥", "API key")}</dd>
          </dl>
        </SettingDetails>
      </Category>
      <Category title={t("维护与恢复", "Maintenance & recovery")}>
        <SettingDetails
          id="record-recycle-bin"
          title={t("记录回收站", "Record recycle bin")}
          summary={
            summary &&
            t(
              `近 7 天 ${summary.recycle} 条记录`,
              `${summary.recycle} records in the last 7 days`,
            )
          }
        >
          <RecycleBin t={t} />
        </SettingDetails>
        <Maintenance t={t} navigate={navigate} reveal={section === "files"} />
        <SettingDetails
          id="backup-restore"
          title={t("备份与恢复", "Backup & restore")}
        >
          <BackupDownload t={t} />
          <p className="small muted">
            {t(
              "备份不含书籍文件，请单独保留。",
              "Backups exclude book files; keep them separately.",
            )}
          </p>
          <details>
            <summary>{t("如何恢复", "How to restore")}</summary>
            <pre>{RESTORE_COMMAND}</pre>
          </details>
        </SettingDetails>
        <Storage t={t} />
      </Category>
      {summary?.version && (
        <footer className="settings-version muted small">
          {t("版本", "Version")} <span>{summary.version}</span>
        </footer>
      )}
    </main>
  );
}
