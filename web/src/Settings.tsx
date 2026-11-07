import { useEffect, useRef, useState, type ReactNode } from "react";
import { ChevronDown } from "lucide-react";
import { Select } from "./Controls";
import { Preferences } from "./PreferenceSettings";
import { Scan } from "./Scan";
import {
  SourceReviewList,
  DuplicateSettings,
  TranslationSettings,
} from "./LibrarySettings";
import { MetadataSync } from "./MetadataSync";
import { SourcesSettings } from "./Sources";
import { Devices } from "./Devices";
import { Credentials } from "./Credentials";
import { PasscodeChange } from "./Passcode";
import { Maintenance } from "./Maintenance";
import { RecycleBin } from "./RemovalDialog";
import { errorMessage, request } from "./api";
import { Pill } from "./Pills";
import { PollingVisible, usePolling } from "./polling";
import type { Lang, Translate } from "./types";
type Guard = { current: (() => Promise<boolean>) | null };
type Summary = {
  review: number;
  sync: Record<string, number>;
  indexed_pages: number;
  total_pages: number;
  translations: number;
  devices: number;
  recycle: number;
};
type Diagnostics = {
  ready: boolean;
  library_root: string;
  database: string;
  books: number;
};
const RESTORE_COMMAND = `python -m mangacouch restore /path/backup.zip /path/restored-library
python -m mangacouch --db /path/restored-library/library.sqlite serve`;

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
  title,
  summary,
  children,
}: {
  title: string;
  summary?: string | false | null;
  children: ReactNode;
}) {
  const [open, setOpen] = useState(false),
    [visited, setVisited] = useState(false);
  return (
    <details
      className="settings-section"
      onToggle={(event) => {
        const value = event.currentTarget.open;
        setOpen(value);
        if (value) setVisited(true);
      }}
    >
      <summary>
        <span>
          <h3>{title}</h3>
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
function Storage({ t }: { t: Translate }) {
  const [data, setData] = useState<Diagnostics | null>(null);
  const { error } = usePolling(async (signal) => {
    const value = await request<Diagnostics>("/api/diagnostics", { signal });
    if (!signal.aborted) setData(value);
  }, 30_000);
  return (
    <section>
      <h3>{t("存储状态", "Storage status")}</h3>
      {!!error && <p className="error-text">{errorMessage(error, t)}</p>}
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
  t,
  guard,
  navigate,
  lang = "zh",
  changeLanguage,
  logout,
  lock,
}: {
  t: Translate;
  guard: Guard;
  navigate: (route: string) => void;
  lang?: Lang;
  changeLanguage?: (lang: Lang) => void;
  logout: () => void;
  lock: (confirmed: boolean) => void;
}) {
  const [summary, setSummary] = useState<Summary | null>(null);
  usePolling(async (signal) => {
    try {
      const value = await request<Summary>("/api/settings/summary", { signal });
      if (!signal.aborted) setSummary(value);
    } catch (error) {
      // The counts are decoration on the section titles; drop them rather than show an error.
      if (!signal.aborted) setSummary(null);
      throw error;
    }
  }, 15_000);
  const preferenceGuard = useRef<Guard["current"]>(null),
    syncGuard = useRef<Guard["current"]>(null),
    credentialGuard = useRef<Guard["current"]>(null);
  useEffect(() => {
    guard.current = async () => {
      for (const part of [preferenceGuard, syncGuard, credentialGuard])
        if (part.current && !(await part.current())) return false;
      return true;
    };
    return () => {
      guard.current = null;
    };
  }, [guard]);
  const sync = summary?.sync ?? {};
  return (
    <main className="content settings-page">
      <span className="eyebrow">{t("偏好与管理", "SETTINGS")}</span>
      <h1>{t("设置", "Settings")}</h1>
      <Category title={t("阅读与显示", "Reading & display")}>
        <label>
          {t("界面语言", "Interface language")}
          <Select
            value={lang}
            onChange={(event) => changeLanguage?.(event.target.value as Lang)}
          >
            <option value="zh">中文</option>
            <option value="en">English</option>
          </Select>
        </label>
        <Preferences t={t} guard={preferenceGuard} />
      </Category>
      <Category title={t("书库管理", "Library management")}>
        <SettingDetails title={t("扫描书库", "Scan library")}>
          <Scan t={t} navigate={navigate} />
        </SettingDetails>
        <SettingDetails
          title={t("本子去重", "Duplicate books")}
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
          title={t("来源核对", "Source review")}
          summary={
            summary &&
            t(
              `${summary.review} 本待核对`,
              `${summary.review} books awaiting review`,
            )
          }
        >
          <SourceReviewList t={t} navigate={navigate} />
        </SettingDetails>
        <SettingDetails
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
      </Category>
      <Category title={t("在线来源", "Online sources")}>
        <SettingDetails
          title={t("来源账户与下载", "Source accounts & downloads")}
        >
          <SourcesSettings t={t} />
        </SettingDetails>
      </Category>
      <Category title={t("访问与连接", "Access & connections")}>
        <SettingDetails
          title={t("网页设备", "Web devices")}
          summary={
            summary &&
            t(
              `${summary.devices} 台已登录设备`,
              `${summary.devices} signed-in devices`,
            )
          }
        >
          <Devices t={t} logout={logout} />
        </SettingDetails>
        <SettingDetails title="API keys">
          <Credentials t={t} guard={credentialGuard} />
        </SettingDetails>
        <SettingDetails title={t("修改书库口令", "Change library passcode")}>
          <PasscodeChange t={t} lock={lock} />
        </SettingDetails>
        <SettingDetails
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
            <dd>Bearer</dd>
            <dt>{t("密码", "Password")}</dt>
            <dd>API Key</dd>
          </dl>
        </SettingDetails>
      </Category>
      <Category title={t("维护与恢复", "Maintenance & recovery")}>
        <SettingDetails
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
        <SettingDetails title={t("检查缺失文件", "Check missing files")}>
          <Maintenance t={t} navigate={navigate} />
        </SettingDetails>
        <SettingDetails title={t("备份与恢复", "Backup & restore")}>
          <a className="button" href="/api/snapshots" download>
            {t("下载书库备份", "Download library backup")}
          </a>
          <h4>{t("数据库恢复方式", "Database restoration")}</h4>
          <p className="small muted">
            {t(
              "在服务器上将备份恢复到新目录，再以恢复后的数据库启动。备份包含记录，原始书籍文件需单独保留。",
              "Restore to a new directory on the server, then start with the restored database. Backups contain records; retain the original book files separately.",
            )}
          </p>
          <pre>{RESTORE_COMMAND}</pre>
        </SettingDetails>
        <Storage t={t} />
      </Category>
    </main>
  );
}
