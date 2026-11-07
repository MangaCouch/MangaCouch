import { Select } from "./Controls";
import { BackButton } from "./navigation";
import { useEffect, useRef, useState } from "react";
import { BookOpen, CalendarDays, CheckCheck, LibraryBig } from "lucide-react";
import { aborted, errorMessage, request } from "./api";
import { Cover } from "./Cover";
import { Pill, languageLabel } from "./Pills";
import { useBookTitle } from "./PreferenceContext";
import type { Book, Translate } from "./types";

type Entry = {
  id: number;
  created_at: number;
  page: number | null;
  completed: boolean;
  kind: string;
  book: Book;
};
type Stats = {
  library: {
    books: number;
    pages: number;
    bytes: number;
    completed: number;
    in_progress: number;
    rated: number;
  };
  period: {
    days: number;
    books: number;
    active_days: number;
    completed: number;
    updates: number;
  };
  daily: { day: string; books: number; updates: number }[];
  languages: { language: string; books: number }[];
};
export function Activity({
  t,
  navigate,
}: {
  t: Translate;
  navigate: (route: string) => void;
}) {
  const title = useBookTitle();
  const [stats, setStats] = useState<Stats | null>(null);
  const [events, setEvents] = useState<Entry[]>([]);
  const [before, setBefore] = useState<string | null>(null);
  const [cursor, setCursor] = useState("");
  const [days, setDays] = useState(30);
  const [busy, setBusy] = useState(true);
  const [error, setError] = useState<unknown>(null);
  const [retry, setRetry] = useState(0);
  const chart = useRef<HTMLDivElement>(null);
  useEffect(() => {
    if (chart.current) chart.current.scrollLeft = chart.current.scrollWidth;
  }, [stats]);
  useEffect(() => {
    const controller = new AbortController();
    void request<Stats>(
      `/api/statistics?days=${days}&utc_offset=${-new Date().getTimezoneOffset()}`,
      { signal: controller.signal },
    )
      .then((value) => {
        if (!controller.signal.aborted) setStats(value);
      })
      .catch((e) => {
        if (!aborted(e)) setError(e);
      });
    return () => controller.abort();
  }, [days, retry]);
  useEffect(() => {
    const controller = new AbortController();
    setBusy(true);
    setError(null);
    void request<{ events: Entry[]; before: string | null }>(
      `/api/reading-history?limit=50&before=${encodeURIComponent(cursor)}&utc_offset=${-new Date().getTimezoneOffset()}`,
      { signal: controller.signal },
    )
      .then((value) => {
        if (!controller.signal.aborted) {
          setEvents((old) => {
            if (!cursor) return value.events;
            const seen = new Set(old.map((entry) => entry.id));
            return [
              ...old,
              ...value.events.filter((entry) => !seen.has(entry.id)),
            ];
          });
          setBefore(value.before);
        }
      })
      .catch((e) => {
        if (!aborted(e)) setError(e);
      })
      .finally(() => {
        if (!controller.signal.aborted) setBusy(false);
      });
    return () => controller.abort();
  }, [cursor, retry]);
  const locale = t("zh-CN", "en-US");
  const number = (value: number) => value.toLocaleString(locale);
  const max = Math.max(1, ...(stats?.daily.map((day) => day.books) ?? [1]));
  return (
    <main className="content activity-page">
      <BackButton t={t} />
      <div className="activity-heading">
        <div>
          <span className="eyebrow">{t("我的阅读", "YOUR READING")}</span>
          <h1>{t("阅读记录", "Reading activity")}</h1>
        </div>
        <Select
          aria-label={t("统计范围", "Statistics period")}
          value={days}
          onChange={(e) => setDays(Number(e.target.value))}
        >
          {[7, 30, 90, 365].map((n) => (
            <option key={n} value={n}>
              {t(`最近 ${n} 天`, `Last ${n} days`)}
            </option>
          ))}
        </Select>
      </div>
      {!!error && (
        <p className="notice error" role="alert">
          {errorMessage(error, t)}
          <button onClick={() => setRetry((n) => n + 1)}>
            {t("重试", "Retry")}
          </button>
        </p>
      )}
      {stats && (
        <>
          <div className="stat-grid">
            {[
              [LibraryBig, t("书库藏书", "Library books"), stats.library.books],
              [
                BookOpen,
                t("期间阅读书籍", "Books in period"),
                stats.period.books,
              ],
              [
                CalendarDays,
                t("活跃天数", "Active days"),
                stats.period.active_days,
              ],
              [
                CheckCheck,
                t("期间标记读完", "Finished in period"),
                stats.period.completed,
              ],
            ].map(([Icon, label, value]) => {
              const Symbol = Icon as typeof BookOpen;
              return (
                <div className="stat-card panel" key={String(label)}>
                  <Symbol size={22} />
                  <strong>{number(value as number)}</strong>
                  <span>{String(label)}</span>
                </div>
              );
            })}
          </div>
          <section className="panel activity-chart">
            <h2>{t("阅读足迹", "Reading over time")}</h2>
            <p className="muted small">
              {t(
                "每天有阅读进度变化的不同书籍数",
                "Distinct books with progress changes each day",
              )}
            </p>
            {stats.period.active_days ? (
              <div
                ref={chart}
                className="activity-bars"
                role="list"
                aria-label={t("每日阅读书籍数", "Books read per day")}
              >
                {stats.daily.map((day) => (
                  <div
                    role="listitem"
                    className="activity-day"
                    key={day.day}
                    title={`${day.day}: ${day.books}`}
                  >
                    <span>{day.books}</span>
                    <i
                      style={{
                        height: `${Math.max(4, (day.books / max) * 100)}px`,
                      }}
                    />
                    <small>{day.day.slice(5)}</small>
                  </div>
                ))}
              </div>
            ) : (
              <p className="muted">
                {t(
                  "这个时间段还没有阅读记录。",
                  "No reading activity in this period.",
                )}
              </p>
            )}
            <p className="library-summary">
              <Pill>
                {t("全库", "Library")}: {number(stats.library.pages)}{" "}
                {t("页", "pages")}
              </Pill>
              <Pill>{(stats.library.bytes / 1024 ** 3).toFixed(2)} GiB</Pill>
              <Pill>
                {t("已读完", "Finished")} {stats.library.completed}
              </Pill>
              <Pill>
                {t("阅读中", "In progress")} {stats.library.in_progress}
              </Pill>
              <Pill>
                {t("已评分", "Rated")} {stats.library.rated}
              </Pill>
            </p>
            <div className="language-stats">
              {stats.languages.map((item) => (
                <span key={item.language}>
                  {languageLabel(item.language, t)} <b>{item.books}</b>
                </span>
              ))}
            </div>
          </section>
        </>
      )}
      <section className="history-section">
        <h2>{t("最近阅读历史", "Recent reading history")}</h2>
        <p className="muted small">
          {t(
            "按天显示每本书最后保存的进度。不推算阅读时长；批量标记不计入。",
            "Latest saved progress per book per day. No estimated reading time; bulk marking is not counted.",
          )}
        </p>
        <div className="history-list">
          {events.map((entry) => (
            <button
              className="history-entry"
              key={entry.id}
              onClick={() => navigate(`read/${entry.book.id}`)}
            >
              <Cover id={entry.book.id} sha256={entry.book.content.sha256} />
              <span>
                <strong>{title(entry.book)}</strong>
                <small>
                  <Pill>
                    {new Date(entry.created_at * 1000).toLocaleString(locale)}
                  </Pill>
                  <Pill>
                    {entry.kind === "reset"
                      ? t("重置进度", "Progress reset")
                      : entry.completed
                        ? t("已读完", "Finished")
                        : `${(entry.page ?? 0) + 1} / ${entry.book.content.page_count}`}
                  </Pill>
                </small>
              </span>
              <BookOpen size={20} />
            </button>
          ))}
        </div>
        {!events.length && !busy && (
          <div className="empty">
            <BookOpen size={30} />
            <p>
              {t(
                "打开一本书，留下新的阅读记录。",
                "Open a book to start your reading history.",
              )}
            </p>
          </div>
        )}
        <div className="load-more">
          {busy ? (
            <p role="status">{t("正在载入…", "Loading…")}</p>
          ) : (
            before !== null && (
              <button onClick={() => setCursor(before)}>
                {t("更早的记录", "Earlier activity")}
              </button>
            )
          )}
        </div>
      </section>
    </main>
  );
}
