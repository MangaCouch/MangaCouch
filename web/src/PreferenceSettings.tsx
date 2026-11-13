import type { GuardRef } from "./navigation";
import { useState, useRef, useEffect } from "react";
import { Select } from "./Controls";
import { usePreferences } from "./PreferenceContext";
import type {
  Preferences as Values,
  PreferenceSnapshot,
} from "./PreferenceContext";
import type { Translate } from "./types";
import { errorMessage, put, request } from "./api";
export function Preferences({ t, guard }: { t: Translate; guard?: GuardRef }) {
  const context = usePreferences();
  const [busy, setBusy] = useState(false),
    [error, setError] = useState<unknown>(null);
  const value = context.saved.settings;
  const saving = useRef(false);
  useEffect(() => {
    if (guard) guard.current = async () => !saving.current;
    return () => {
      if (guard) guard.current = null;
    };
  }, [guard]);
  async function save(next: Values) {
    if (saving.current) return;
    saving.current = true;
    setBusy(true);
    setError(null);
    try {
      context.accept(
        await put<PreferenceSnapshot>(
          `/api/preferences?expected_revision=${context.saved.revision}`,
          { settings: next },
        ),
      );
    } catch (error) {
      setError(error);
      // Show what the server holds now; if that read fails too, the error above stays.
      await request<PreferenceSnapshot>("/api/preferences").then(
        context.accept,
        () => {},
      );
    } finally {
      saving.current = false;
      setBusy(false);
    }
  }
  return (
    <section className="preferences">
      {!!error && (
        <p className="notice error" role="alert">
          {errorMessage(error, t)}
        </p>
      )}
      <label className="settings-field">
        {t("标题显示", "Title display")}
        <Select
          aria-label={t("标题显示", "Title display")}
          value={value.title_preference}
          disabled={busy || !context.loaded}
          onChange={(event) =>
            void save({
              ...value,
              title_preference: event.target
                .value as Values["title_preference"],
            })
          }
        >
          <option value="original_title">
            {t("原始优先", "Original first")}
          </option>
          <option value="title">{t("翻译优先", "Translation first")}</option>
        </Select>
      </label>
      <span role="status" className="small muted">
        {busy ? t("正在保存…", "Saving…") : ""}
      </span>
    </section>
  );
}
