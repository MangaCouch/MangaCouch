import { BulkActions } from "./BulkActions";
import {
  advanceTrail,
  NavigationContext,
  parentRoute,
  rememberTrail,
  restoreTrail,
  validRoute,
} from "./navigation";
import { Notifications, toast } from "./feedback";
import {
  lazy,
  Suspense,
  useCallback,
  useEffect,
  useRef,
  useState,
} from "react";
import {
  Languages,
  LogOut,
  ListChecks,
  Settings2,
  History,
} from "lucide-react";
import { aborted, post, request } from "./api";
import { initialBrowse } from "./LibraryFilters";
import { Modal } from "./Modal";
import { type LibrarySelection } from "./BookShelf";
import { TaskQueue } from "./TaskQueue";
import { preference, remember } from "./device";
import { PreferenceSession } from "./PreferenceContext";
import { Login } from "./Login";
import { Library } from "./Library";
import type { Lang, Translate } from "./types";

const Collections = lazy(() =>
  import("./Collections").then((module) => ({ default: module.Collections })),
);
const Downloads = lazy(() =>
  import("./Downloads").then((module) => ({ default: module.Downloads })),
);
const Activity = lazy(() =>
  import("./Activity").then((module) => ({ default: module.Activity })),
);
const Settings = lazy(() =>
  import("./Settings").then((module) => ({ default: module.Settings })),
);
const BookView = lazy(() =>
  import("./BookView").then((module) => ({ default: module.BookView })),
);
type Guard = () => Promise<boolean>;
const routeNow = () => (validRoute(location.hash) ? location.hash : "");
const navigate = (route: string) => {
  location.hash = route;
};
/** Transient notices are toasts; this id lets a later state replace or dismiss them. */
const NOTICE_TOAST = "app-notice";
const notify = (text: string) =>
  toast(text, { id: NOTICE_TOAST, duration: 8000 });

export function App() {
  const [lang, setLang] = useState<Lang>(
    preference("language", "zh") === "en" ? "en" : "zh",
  );
  const t = useCallback<Translate>(
    (zh, en) => (lang === "zh" ? zh : en),
    [lang],
  );
  // Handlers registered once still translate with the current language.
  const translate = useRef(t);
  translate.current = t;
  const [auth, setAuthValue] = useState<"checking" | "locked" | "ready">(
    "checking",
  );
  const authRef = useRef(auth);
  const setAuth = useCallback((value: typeof auth) => {
    authRef.current = value;
    setAuthValue(value);
  }, []);
  /** Why the session ended; shown inline on the login screen only. */
  const [loginNotice, setLoginNotice] = useState("");
  const [libraryVersion, setLibraryVersion] = useState(0);
  const [confirmLogout, setConfirmLogout] = useState(false);
  const [loggingOut, setLoggingOut] = useState(false);
  const changeLanguage = (value: Lang) => {
    setLang(value);
    remember("language", value);
  };
  useEffect(() => {
    document.documentElement?.setAttribute(
      "lang",
      lang === "zh" ? "zh-CN" : "en",
    );
  }, [lang]);
  const [route, setRoute] = useState(routeNow);
  const [trail, setTrail] = useState(() => restoreTrail(route));
  useEffect(() => rememberTrail(trail), [trail]);
  const [browse, setBrowse] = useState(initialBrowse);
  const [librarySelection, setLibrarySelection] =
    useState<LibrarySelection | null>(null);
  const [selectRequested, setSelectRequested] = useState(false);
  const guard = useRef<Guard | null>(null);
  const active = useRef(route);
  const sequence = useRef(0);
  const historyPosition = useRef(0);
  useEffect(() => {
    history.replaceState(
      { ...history.state, mcPosition: historyPosition.current },
      "",
      location.pathname + location.search + active.current,
    );
    const change = async () => {
      const count = ++sequence.current;
      const next = routeNow();
      if (next === active.current) return;
      const position =
        typeof history.state?.mcPosition === "number"
          ? history.state.mcPosition
          : historyPosition.current + 1;
      history.replaceState(
        { ...history.state, mcPosition: position },
        "",
        location.pathname + location.search + next,
      );
      const allowed = await (guard.current?.() ?? Promise.resolve(true));
      if (sequence.current !== count) return;
      if (!allowed) {
        setSelectRequested(false);
        history.go(historyPosition.current - position);
        notify(
          translate.current(
            "请先完成正在进行的操作或处理未保存的修改，再离开此页。",
            "Finish the current operation or resolve unsaved changes before leaving.",
          ),
        );
        return;
      }
      toast.dismiss(NOTICE_TOAST);
      if (next && next !== "#") setSelectRequested(false);
      historyPosition.current = position;
      active.current = next;
      setTrail((old) => advanceTrail(old, next));
      setRoute(next);
    };
    window.addEventListener("hashchange", change);
    return () => window.removeEventListener("hashchange", change);
  }, []);
  useEffect(() => {
    const locked = () => {
      sequence.current++;
      guard.current = null;
      // Only a session that was signed in can expire; the first anonymous check is a
      // normal 401 and just shows the login form.
      if (authRef.current === "ready")
        setLoginNotice(
          translate.current(
            "登录已过期，尚未保存的修改可能未保存。",
            "Session expired; pending changes may not have been saved.",
          ),
        );
      setAuth("locked");
    };
    window.addEventListener("mc-locked", locked);
    const controller = new AbortController();
    void request("/api/auth/me", { signal: controller.signal })
      .then(() => setAuth("ready"))
      .catch((error) => {
        if (!aborted(error)) setAuth("locked");
      });
    return () => {
      controller.abort();
      window.removeEventListener("mc-locked", locked);
    };
  }, [setAuth]);
  useEffect(() => {
    if (auth !== "ready") return;
    let running = false;
    const controller = new AbortController();
    const renew = async () => {
      if (document.hidden || running || controller.signal.aborted) return;
      running = true;
      try {
        const session = await request<{ rotation_due: boolean }>(
          "/api/auth/me",
          { signal: controller.signal },
        );
        if (session.rotation_due)
          await request("/api/auth/rotate", {
            method: "POST",
            signal: controller.signal,
          });
      } catch {
        /* A 401 locks via the shared request handler; transient failures retry. */
      } finally {
        running = false;
      }
    };
    const timer = setInterval(() => void renew(), 60_000);
    document.addEventListener("visibilitychange", renew);
    return () => {
      controller.abort();
      clearInterval(timer);
      document.removeEventListener("visibilitychange", renew);
    };
  }, [auth]);
  const search = (query: string) => {
    setBrowse((old) => ({ ...old, query }));
    navigate("");
  };
  const reading = route.startsWith("#read/");
  const parts = route.slice(1).split("/");
  const id = parts[0] === "book" || parts[0] === "read" ? parts[1] : undefined;
  const collectionId = parts[2] === "in" ? parts[3] : undefined;
  async function logout() {
    if (guard.current && !(await guard.current())) {
      notify(
        t(
          "请先保存或处理未保存的修改。",
          "Save or resolve your unsaved changes first.",
        ),
      );
      return;
    }
    if (loggingOut) return;
    setLoggingOut(true);
    setConfirmLogout(false);
    setAuth("locked");
    setLoginNotice("");
    toast.dismiss(NOTICE_TOAST);
    try {
      await post("/api/auth/logout");
    } catch {
      setLoginNotice(
        t(
          "本页已锁定，但服务器退出未确认。请重新登录后再次退出。",
          "This page is locked, but server sign-out is unconfirmed. Sign in and sign out again.",
        ),
      );
    } finally {
      setLoggingOut(false);
    }
  }
  return (
    <div className="app-shell" lang={lang}>
      <header
        className={`site-head ${reading && auth === "ready" ? "reading-head" : ""}`}
      >
        <button className="wordmark" onClick={() => navigate("")}>
          <img src="/assets/icon.svg" width={26} height={26} alt="" />
          MangaCouch
        </button>
        <div className="actions">
          {auth !== "ready" && (
            <button
              className="text-button"
              aria-label={t("切换语言", "Switch language")}
              onClick={() => {
                const value = lang === "zh" ? "en" : "zh";
                changeLanguage(value);
              }}
            >
              <Languages size={17} />
              {lang === "zh" ? "EN" : "中文"}
            </button>
          )}
          {auth === "ready" && (
            <>
              <TaskQueue t={t} navigate={navigate} />
              <button
                className="icon-button"
                aria-label={t("整理", "Organize")}
                onClick={() => {
                  setSelectRequested(true);
                  navigate("");
                }}
              >
                <ListChecks size={18} />
              </button>
              <button
                className="icon-button"
                aria-label={t("阅读历史与统计", "Reading history & statistics")}
                onClick={() => navigate("activity")}
              >
                <History size={18} />
              </button>

              <button
                className="icon-button"
                aria-label={t("设置", "Settings")}
                onClick={() => navigate("settings")}
              >
                <Settings2 size={18} />
              </button>
            </>
          )}
          {auth === "ready" && (
            <button
              className="icon-button"
              aria-label={t("退出登录", "Sign out")}
              onClick={() => setConfirmLogout(true)}
            >
              <LogOut size={18} />
            </button>
          )}
        </div>
      </header>
      {auth === "checking" ? (
        <main className="empty" role="status">
          {t("正在打开书库…", "Opening library…")}
        </main>
      ) : auth === "locked" ? (
        <Login
          t={t}
          notice={loginNotice}
          entered={() => {
            setAuth("ready");
            setLoginNotice("");
            toast.dismiss(NOTICE_TOAST);
          }}
        />
      ) : (
        <NavigationContext.Provider
          value={{ target: trail.at(-2) ?? parentRoute(route), navigate }}
        >
          <PreferenceSession t={t}>
            <Suspense
              fallback={
                <main className="content" role="status">
                  {t("正在打开…", "Opening…")}
                </main>
              }
            >
              {id ? (
                <BookView
                  key={`${id}:${collectionId || ""}`}
                  id={id}
                  collectionId={collectionId}
                  initialPage={
                    route.includes("/page/")
                      ? Number(route.split("/page/")[1])
                      : undefined
                  }
                  reading={reading}
                  t={t}
                  guard={guard}
                  navigate={navigate}
                  search={search}
                />
              ) : parts[0] === "collections" || parts[0] === "collection" ? (
                <Collections
                  key={parts[1] || "index"}
                  id={parts[0] === "collection" ? parts[1] : undefined}
                  t={t}
                  guard={guard}
                  navigate={navigate}
                />
              ) : parts[0] === "activity" ? (
                <Activity t={t} navigate={navigate} />
              ) : parts[0] === "downloads" ? (
                <Downloads t={t} navigate={navigate} />
              ) : parts[0] === "settings" ? (
                <Settings
                  t={t}
                  guard={guard}
                  navigate={navigate}
                  lang={lang}
                  changeLanguage={changeLanguage}
                  logout={() => void logout()}
                  lock={(confirmed) => {
                    sequence.current++;
                    guard.current = null;
                    setLoginNotice(
                      confirmed
                        ? t(
                            "口令已更新，请重新登录。",
                            "Passcode updated. Sign in again.",
                          )
                        : t(
                            "请尝试新口令重新登录。",
                            "Try signing in with the new passcode.",
                          ),
                    );
                    setAuth("locked");
                  }}
                />
              ) : (
                <Library
                  t={t}
                  guard={guard}
                  navigate={navigate}
                  version={libraryVersion}
                  browse={browse}
                  onBrowse={setBrowse}
                  selectRequested={selectRequested}
                  onSelectionStarted={() => setSelectRequested(false)}
                  onOrganize={(selection) => {
                    setLibrarySelection(selection);
                  }}
                />
              )}
              {librarySelection && (
                <BulkActions
                  selection={librarySelection}
                  t={t}
                  close={() => setLibrarySelection(null)}
                  changed={() => setLibraryVersion((value) => value + 1)}
                />
              )}
            </Suspense>
          </PreferenceSession>
        </NavigationContext.Provider>
      )}
      <Notifications t={t} />
      {confirmLogout && (
        <Modal
          title={t("退出登录？", "Sign out?")}
          closeLabel={t("取消", "Cancel")}
          close={() => setConfirmLogout(false)}
          busy={loggingOut}
        >
          <div className="actions modal-actions">
            <button onClick={() => setConfirmLogout(false)}>
              {t("取消", "Cancel")}
            </button>
            <button
              className="primary"
              disabled={loggingOut}
              onClick={() => void logout()}
            >
              {t("退出登录", "Sign out")}
            </button>
          </div>
        </Modal>
      )}
    </div>
  );
}
