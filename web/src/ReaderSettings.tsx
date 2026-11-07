import { X } from "lucide-react";
import { Select } from "./Controls";
import type { ReadingMode } from "./reading";
import type { ReadingDirection, ReadingFit } from "./device";
import type { Translate } from "./types";

/** The reader's settings panel; every change is reported through `change`. */
export function ReaderSettings({
  open,
  close,
  mode,
  coverSolo,
  fit,
  direction,
  change,
  retryPreload,
  t,
}: {
  open: boolean;
  close: () => void;
  mode: ReadingMode;
  coverSolo: boolean;
  fit: ReadingFit;
  direction: ReadingDirection;
  change: (
    value:
      | { mode: ReadingMode }
      | { coverSolo: boolean }
      | { fit: ReadingFit }
      | { direction: ReadingDirection },
  ) => void;
  /** Present when the background preload stopped and can be retried. */
  retryPreload?: () => void;
  t: Translate;
}) {
  return (
    <>
      {open && (
        <button
          type="button"
          className="settings-dismiss"
          aria-label={t("关闭阅读设置", "Close reading settings")}
          onClick={close}
        />
      )}
      <section
        hidden={!open}
        className="reader-settings glass"
        aria-label={t("阅读设置", "Reading settings")}
      >
        <header className="reader-settings-title">
          <h2>{t("阅读设置", "Reading settings")}</h2>
          <button
            type="button"
            className="icon-button"
            aria-label={t("关闭", "Close")}
            onClick={close}
          >
            <X size={18} />
          </button>
        </header>
        <label>
          {t("阅读模式", "Reading mode")}
          <Select
            aria-label={t("阅读模式", "Reading mode")}
            value={mode}
            onChange={(event) =>
              change({ mode: event.target.value as ReadingMode })
            }
          >
            <option value="single">{t("单页", "Single page")}</option>
            <option value="spread">{t("双页", "Two pages")}</option>
            <option value="scroll">{t("纵向滚动", "Vertical scroll")}</option>
          </Select>
        </label>
        {mode === "spread" && (
          <label className="checkbox">
            <input
              type="checkbox"
              checked={coverSolo}
              onChange={(event) => change({ coverSolo: event.target.checked })}
            />
            {t("封面单独显示", "Show cover on its own")}
          </label>
        )}
        <label>
          {t("适应", "Fit")}
          <Select
            value={fit}
            onChange={(event) =>
              change({ fit: event.target.value as ReadingFit })
            }
          >
            <option value="height">{t("适应高度", "Fit height")}</option>
            <option value="width">{t("宽度", "Width")}</option>
          </Select>
        </label>
        <label>
          {t("阅读方向", "Direction")}
          <Select
            value={direction}
            aria-label={t("阅读方向", "Direction")}
            disabled={mode === "scroll"}
            onChange={(event) =>
              change({ direction: event.target.value as ReadingDirection })
            }
          >
            <option value="ltr">{t("从左到右", "Left to right")}</option>
            <option value="rtl">{t("从右到左", "Right to left")}</option>
          </Select>
        </label>
        {retryPreload && (
          <button type="button" className="subtle" onClick={retryPreload}>
            {t("重试预加载", "Retry preload")}
          </button>
        )}
      </section>
    </>
  );
}
