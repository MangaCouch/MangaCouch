import { createContext, useContext, useEffect, useMemo, useState } from "react";
import type { ReactNode } from "react";
import { aborted, errorMessage, request } from "./api";
import { title } from "./types";
import type { Translate } from "./types";

/** Preferences shared by every device; reader settings stay with each device (device.ts). */
export type Preferences = {
  title_preference: "title" | "original_title";
};
export type PreferenceSnapshot = { revision: number; settings: Preferences };
export const defaultPreferences: PreferenceSnapshot = {
  revision: 0,
  settings: { title_preference: "title" },
};
export const PreferencesContext = createContext({
  saved: defaultPreferences,
  /** False until the server snapshot arrived; `saved` holds defaults until then. */
  loaded: true,
  accept: (_value: PreferenceSnapshot) => {},
});
export const usePreferences = () => useContext(PreferencesContext);
export function useBookTitle() {
  const { saved } = usePreferences();
  return (book: Parameters<typeof title>[0]) =>
    title(book, saved.settings.title_preference);
}
/**
 * Provides the shared preferences. Children render at once with the defaults; the server
 * snapshot replaces them when it arrives.
 */
export function PreferenceSession({
  t,
  children,
}: {
  t: Translate;
  children: ReactNode;
}) {
  const [saved, setSaved] = useState<PreferenceSnapshot | null>(null);
  const [error, setError] = useState<unknown>(null);
  const [attempt, setAttempt] = useState(0);
  useEffect(() => {
    const controller = new AbortController();
    setError(null);
    void request<PreferenceSnapshot>("/api/preferences", {
      signal: controller.signal,
    })
      .then((value) => {
        if (controller.signal.aborted) return;
        setSaved(value);
      })
      .catch((error) => {
        if (!aborted(error) && !controller.signal.aborted) setError(error);
      });
    return () => controller.abort();
  }, [attempt]);
  const value = useMemo(
    () => ({
      saved: saved ?? defaultPreferences,
      loaded: !!saved,
      accept: setSaved,
    }),
    [saved],
  );
  return (
    <PreferencesContext.Provider value={value}>
      {!saved && !!error && (
        <div className="notice error" role="alert">
          {errorMessage(error, t)}{" "}
          <button type="button" onClick={() => setAttempt((n) => n + 1)}>
            {t("重试读取偏好", "Retry preferences")}
          </button>
        </div>
      )}
      {children}
    </PreferencesContext.Provider>
  );
}
