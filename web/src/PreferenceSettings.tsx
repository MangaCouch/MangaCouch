import { useState, useRef, useEffect } from "react";
import { Select } from "./Controls";
import { usePreferences } from "./PreferenceContext";
import type {
  Preferences as Values,
  PreferenceSnapshot,
} from "./PreferenceContext";
import { AUTOPLAY_SECONDS, deviceKeys, remember } from "./device";
import type { DeviceDefaults } from "./device";
import type { Translate } from "./types";
import { errorMessage, put, request } from "./api";
export function Preferences({
  t,
  guard,
}: {
  t: Translate;
  guard?: { current: (() => Promise<boolean>) | null };
}) {
  const context = usePreferences();
  const [busy, setBusy] = useState(false),
    [error, setError] = useState<unknown>(null),
    [applied, setApplied] = useState(false);
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
    setApplied(false);
    try {
      context.accept(
        await put<PreferenceSnapshot>("/api/preferences", {
          settings: next,
          expected_revision: context.saved.revision,
        }),
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
  const choice = <K extends keyof DeviceDefaults>(
    key: K,
    label: string,
    options: [DeviceDefaults[K], string][],
  ) => (
    <label>
      {label}
      <Select
        value={String(
          key === "autoplay_seconds" &&
            !(AUTOPLAY_SECONDS as readonly number[]).includes(
              Number(value.device_defaults[key]),
            )
            ? 5
            : value.device_defaults[key],
        )}
        disabled={busy || !context.loaded}
        aria-label={label}
        onChange={(event) => {
          const chosen = options.find(
            ([v]) => String(v) === event.target.value,
          )?.[0];
          if (chosen !== undefined)
            void save({
              ...value,
              device_defaults: { ...value.device_defaults, [key]: chosen },
            });
        }}
      >
        {options.map(([v, text]) => (
          <option key={String(v)} value={String(v)}>
            {text}
          </option>
        ))}
      </Select>
    </label>
  );
  return (
    <section>
      {!!error && <p className="notice error">{errorMessage(error, t)}</p>}
      <label>
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
      <h3>{t("新设备默认值", "New device defaults")}</h3>
      <p className="small muted">
        {t(
          "这些值只用于首次打开阅读器的设备；已用过的设备保留自己的阅读设置。",
          "These apply to devices that have not opened the reader yet; devices already in use keep their own reading settings.",
        )}{" "}
        <button
          type="button"
          className="text-button"
          disabled={!context.loaded}
          onClick={() => {
            const defaults = value.device_defaults;
            for (const key of Object.keys(
              deviceKeys,
            ) as (keyof DeviceDefaults)[])
              remember(deviceKeys[key], String(defaults[key]));
            setApplied(true);
          }}
        >
          {t("应用到此设备", "Apply to this device")}
        </button>
      </p>
      <div className="preference-fields">
        {choice("reading_mode", t("阅读模式", "Reading mode"), [
          ["single", t("单页", "Single page")],
          ["spread", t("双页", "Spread")],
          ["scroll", t("纵向滚动", "Vertical scroll")],
        ])}
        {choice("fit", t("适应", "Fit"), [
          ["height", t("适应高度", "Height")],
          ["width", t("适应宽度", "Width")],
        ])}
        {choice("direction", t("阅读方向", "Direction"), [
          ["ltr", t("从左到右", "Left to right")],
          ["rtl", t("从右到左", "Right to left")],
        ])}
        {choice("cover_solo", t("封面单独显示", "Cover on its own"), [
          [true, t("是", "Yes")],
          [false, t("否", "No")],
        ])}
        {choice(
          "autoplay_seconds",
          t("自动播放间隔", "Autoplay interval"),
          AUTOPLAY_SECONDS.map((n) => [n, `${n}s`]),
        )}
      </div>
      <span role="status" className="small muted">
        {busy
          ? t("正在保存…", "Saving…")
          : applied
            ? t("已应用到此设备。", "Applied to this device.")
            : ""}
      </span>
    </section>
  );
}
