import { RouteLink } from "./navigation";
import {
  advanceTrail,
  defaultTrail,
  NavigationContext,
  parentRoute,
  rememberTrail,
  restoreTrail,
  returnRoute,
  validRoute,
} from "./navigation";
import { EDIT_INTENT } from "./bookActions";
import { initialBrowse } from "./LibraryFilters";
import type { Guard } from "./navigation";
import { Notifications, toast } from "./feedback";
import { ErrorBoundary } from "./ErrorBoundary";
import {
  lazy,
  Suspense,
  useCallback,
  useEffect,
  useMemo,
  useRef,
  useState,
} from "react";
import {
  Languages,
  LogOut,
  Settings as SettingsIcon,
  History,
} from "lucide-react";
import { aborted, ApiError, errorMessage, post, request } from "./api";
import {
  librarySearch,
  rememberLibraryView,
  restoreLibraryView,
} from "./libraryView";
import { Modal } from "./Modal";
import { TaskProvider, TaskQueue } from "./TaskQueue";
import { browserLanguage, preference, remember } from "./device";
import { PreferenceSession } from "./PreferenceContext";
import { Login } from "./Login";
import { Library } from "./Library";
import type { LibraryScroll } from "./Library";
import type { ReactNode } from "react";
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
const routeNow = () => (validRoute(location.hash) ? location.hash : "");
const navigate = (route: string) => {
  location.hash = route;
};
/** Transient notices are toasts; this id lets a later state replace or dismiss them. */
const NOTICE_TOAST = "app-notice";
const notify = (text: string) =>
  toast(text, { id: NOTICE_TOAST, duration: 8000 });

/** A header action: an icon with a tooltip everywhere, and its label beside it on wide screens. */
function HeadAction({
  label,
  hint,
  onClick,
  route,
  children,
}: {
  label: string;
  hint?: string;
  onClick?: () => void;
  route?: string;
  children: ReactNode;
}) {
  if (route !== undefined)
    return (
      <RouteLink
        route={route}
        navigate={navigate}
        className="icon-button head-action"
        aria-label={label}
        title={hint ?? label}
      >
        {children}
        <span className="head-label">{label}</span>
      </RouteLink>
    );
  return (
    <button
      className="icon-button head-action"
      aria-label={label}
      title={hint ?? label}
      onClick={onClick}
    >
      {children}
      <span className="head-label">{label}</span>
    </button>
  );
}

export function App() {
  const [lang, setLang] = useState<Lang>(() =>
    preference("language", browserLanguage()) === "en" ? "en" : "zh",
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
  const [connectionError, setConnectionError] = useState<unknown>(null);
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
  // Filters are part of the URL; scroll is attached to its history entry.
  const [startView] = useState(restoreLibraryView);
  const [browse, setBrowse] = useState(startView.browse);
  const [libraryPage, setLibraryPage] = useState(startView.page);
  const libraryScroll = useRef<LibraryScroll | null>(startView.scroll);
  const activeURL = useRef(location.href);
  const observedURL = useRef(location.href);
  useEffect(() => {
    const save = () => {
      if (route || location.hash || location.href !== activeURL.current) return;
      rememberLibraryView({
        browse,
        page: libraryPage,
        scroll: libraryScroll.current,
      });
      activeURL.current = observedURL.current = location.href;
    };
    save();
    window.addEventListener("pagehide", save);
    return () => window.removeEventListener("pagehide", save);
  }, [browse, libraryPage, route]);
  const guard = useRef<Guard | null>(null);
  const active = useRef(route);
  const sequence = useRef(0);
  const historyPosition = useRef(
    typeof history.state?.mcPosition === "number"
      ? history.state.mcPosition
      : 0,
  );
  const browserPosition = useRef(historyPosition.current);
  const acceptedState = useRef(history.state);
  /** An ancestor route a back step is still travelling to through older entries. */
  const walk = useRef<string | null>(null);
  const consumePage = useCallback(() => {
    const current = active.current;
    const next = current.replace(/\/page\/\d+$/, "");
    if (next === current) return;
    history.replaceState(
      history.state,
      "",
      location.pathname + location.search + next,
    );
    active.current = next;
    activeURL.current = observedURL.current = location.href;
    setRoute(next);
    setTrail((old) => old.map((item) => (item === current ? next : item)));
  }, []);
  useEffect(() => {
    if (typeof history.state?.mcPosition !== "number") {
      const parents = defaultTrail(active.current);
      let previous: { url: string; position: number } | null = null;
      parents.forEach((item, position) => {
        const url = location.pathname + location.search + item;
        const state = {
          mcPosition: position,
          mcPrevious: previous,
          mcTrail: parents.slice(0, position + 1),
        };
        if (position === 0) history.replaceState(state, "", url);
        else history.pushState(state, "", url);
        previous = { url: location.href, position };
      });
      setTrail(parents);
      historyPosition.current = browserPosition.current = parents.length - 1;
    }
    activeURL.current = observedURL.current = location.href;
    acceptedState.current = history.state;
    const change = async () => {
      let url = location.href;
      if (url === observedURL.current) return;
      observedURL.current = url;
      const unknown = typeof history.state?.mcPosition !== "number";
      let position = unknown
        ? browserPosition.current + 1
        : history.state.mcPosition;
      browserPosition.current = position;
      let next = routeNow();
      const walking = walk.current;
      walk.current = null;
      // The entry this step back landed on, before it was rewritten to the page it shows.
      let landed: { state: unknown; url: string } | null = null;
      if (!unknown && position < historyPosition.current) {
        const before = history.state.mcPrevious;
        if (
          walking !== null &&
          next !== walking &&
          before &&
          before.position < position &&
          new URL(before.url, location.href).hash === walking
        ) {
          // Passing through on the way back to an ancestor: never shown.
          walk.current = walking;
          history.go(before.position - position);
          return;
        }
        const target =
          walking ??
          (acceptedState.current?.mcPrevious?.position === position
            ? returnRoute(active.current, next)
            : next);
        if (target !== next) {
          landed = { state: history.state, url };
          history.replaceState(
            history.state,
            "",
            location.pathname + location.search + target,
          );
          url = observedURL.current = location.href;
          next = target;
        }
      }
      if (url === activeURL.current) return;
      const count = ++sequence.current;
      history.replaceState({ ...history.state, mcPosition: position }, "", url);
      const allowed = await (guard.current?.(next) ?? Promise.resolve(true));
      if (sequence.current !== count) return;
      if (allowed !== true) {
        try {
          sessionStorage.removeItem(EDIT_INTENT);
        } catch {
          /* Optional storage. */
        }
        if (landed) history.replaceState(landed.state, "", landed.url);
        const delta = historyPosition.current - position;
        if (delta !== 0) history.go(delta);
        else {
          history.replaceState(acceptedState.current, "", activeURL.current);
          observedURL.current = activeURL.current;
          browserPosition.current = historyPosition.current;
        }
        notify(
          typeof allowed === "string"
            ? allowed
            : translate.current(
                "请先完成正在进行的操作或处理未保存的修改，再离开此页。",
                "Finish the current operation or resolve unsaved changes before leaving.",
              ),
        );
        return;
      }
      let previous = unknown
        ? { url: activeURL.current, position: historyPosition.current }
        : history.state?.mcPrevious;
      let nextTrail = history.state?.mcTrail;
      if (
        next.startsWith("#read/") &&
        unknown &&
        active.current !== parentRoute(next)
      ) {
        const parent = parentRoute(next);
        history.replaceState(
          { mcPosition: position, mcPrevious: previous },
          "",
          location.pathname + location.search + parent,
        );
        previous = { url: location.href, position };
        position++;
        history.pushState(
          { mcPosition: position, mcPrevious: previous },
          "",
          url,
        );
        nextTrail = [
          ...advanceTrail(restoreTrail(active.current), parent),
          next,
        ];
      }
      toast.dismiss(NOTICE_TOAST);
      historyPosition.current = browserPosition.current = position;
      active.current = next;
      activeURL.current = observedURL.current = location.href;
      history.replaceState(
        { ...history.state, mcPosition: position, mcPrevious: previous },
        "",
        location.href,
      );
      const updated =
        Array.isArray(nextTrail) && nextTrail.at(-1) === next
          ? nextTrail
          : advanceTrail(
              acceptedState.current?.mcTrail ?? defaultTrail(next),
              next,
            );
      history.replaceState(
        { ...history.state, mcTrail: updated },
        "",
        location.href,
      );
      acceptedState.current = history.state;
      setTrail(updated);
      const view = restoreLibraryView();
      setBrowse(view.browse);
      setLibraryPage(view.page);
      libraryScroll.current = view.scroll;
      setRoute(next);
    };
    window.addEventListener("hashchange", change);
    window.addEventListener("popstate", change);
    return () => {
      window.removeEventListener("hashchange", change);
      window.removeEventListener("popstate", change);
    };
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
    let retry: ReturnType<typeof setTimeout> | undefined;
    let attempts = 0;
    const check = async () => {
      try {
        await request("/api/auth/me", { signal: controller.signal });
        if (!controller.signal.aborted) {
          setConnectionError(null);
          setAuth("ready");
        }
      } catch (error) {
        if (aborted(error) || controller.signal.aborted) return;
        if (error instanceof ApiError && error.status === 401) {
          setAuth("locked");
          return;
        }
        setConnectionError(error);
        retry = setTimeout(
          () => void check(),
          Math.min(8, 2 ** attempts++) * 1000,
        );
      }
    };
    void check();
    return () => {
      controller.abort();
      clearTimeout(retry);
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
    const url =
      location.pathname + librarySearch({ ...initialBrowse(), query });
    // Already showing exactly this search (e.g. the logo on the plain library).
    if (url === location.pathname + location.search + location.hash) return;
    history.pushState(null, "", url);
    window.dispatchEvent(new PopStateEvent("popstate"));
  };
  const reading = route.startsWith("#read/");
  const parts = route.slice(1).split("/");
  const id = parts[0] === "book" || parts[0] === "read" ? parts[1] : undefined;
  const collectionId = parts[2] === "in" ? parts[3] : undefined;
  const backTarget = reading
    ? parentRoute(route)
    : (trail.at(-2) ?? parentRoute(route));
  const navigation = useMemo(
    () => ({
      target: backTarget,
      navigate: (target: string) => {
        const previous = history.state?.mcPrevious;
        if (previous && previous.position < historyPosition.current) {
          const from = active.current;
          const landing = returnRoute(
            from,
            new URL(previous.url, location.href).hash,
          );
          // A reader can also leave for an ancestor of its details (its collection):
          // travel back through the details instead of stacking a new entry.
          const ancestor =
            from.startsWith("#read/") &&
            landing.startsWith("#book/") &&
            defaultTrail(landing).includes(target);
          if (landing === target || ancestor) {
            walk.current = landing === target ? null : target;
            history.go(previous.position - historyPosition.current);
            return;
          }
        }
        history.replaceState(
          { ...history.state, mcPrevious: null },
          "",
          location.pathname + location.search + target,
        );
        window.dispatchEvent(new PopStateEvent("popstate"));
      },
      replace: (route: string) => {
        history.replaceState(
          history.state,
          "",
          location.pathname + location.search + route,
        );
        window.dispatchEvent(new PopStateEvent("popstate"));
      },
    }),
    [backTarget],
  );
  async function logout() {
    const allowed = guard.current ? await guard.current() : true;
    if (allowed !== true) {
      notify(
        typeof allowed === "string"
          ? allowed
          : t(
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
    <TaskProvider enabled={auth === "ready"}>
      <div className="app-shell" lang={lang}>
        <header
          className={`site-head ${reading && auth === "ready" ? "reading-head" : ""}`}
        >
          <RouteLink
            className="wordmark"
            route={""}
            navigate={() => search("")}
          >
            <img src="/assets/icon.svg" width={26} height={26} alt="" />
            MangaCouch
          </RouteLink>
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
                <HeadAction
                  label={t("阅读记录", "Activity")}
                  hint={t("阅读历史与统计", "Reading history & statistics")}
                  route="activity"
                >
                  <History size={18} />
                </HeadAction>
                <HeadAction label={t("设置", "Settings")} route="settings">
                  <SettingsIcon size={18} />
                </HeadAction>
                <HeadAction
                  label={t("退出登录", "Sign out")}
                  onClick={() => setConfirmLogout(true)}
                >
                  <LogOut size={18} />
                </HeadAction>
              </>
            )}
          </div>
        </header>
        {auth === "checking" ? (
          <main className="empty" role="status">
            {connectionError
              ? `${errorMessage(connectionError, t)} ${t("正在重试…", "Retrying…")}`
              : t("正在打开书库…", "Opening library…")}
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
          <NavigationContext.Provider value={navigation}>
            <PreferenceSession t={t}>
              <ErrorBoundary t={t} resetKey={route}>
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
                          ? Math.max(0, Number(route.split("/page/")[1]) - 1)
                          : undefined
                      }
                      consumePage={consumePage}
                      reading={reading}
                      t={t}
                      guard={guard}
                      navigate={navigate}
                      search={search}
                    />
                  ) : parts[0] === "collections" ||
                    parts[0] === "collection" ? (
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
                    <Downloads t={t} navigate={navigate} focusId={parts[1]} />
                  ) : parts[0] === "settings" ? (
                    <Settings
                      section={parts[1]}
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
                                "无法确认口令是否已修改：请先用新口令登录，不行再用原口令。",
                                "Whether the passcode changed is unknown: sign in with the new passcode, or the old one if that fails.",
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
                      browse={browse}
                      onBrowse={setBrowse}
                      page={libraryPage}
                      onPage={setLibraryPage}
                      scroll={libraryScroll}
                    />
                  )}
                </Suspense>
              </ErrorBoundary>
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
    </TaskProvider>
  );
}
