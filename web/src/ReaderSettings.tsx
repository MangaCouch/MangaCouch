import { useEffect, useRef } from "react";
import { X } from "lucide-react";
import { Select } from "./Controls";
import type { ReadingMode } from "./reading";
import type { ReadingDirection, ReadingFit } from "./device";
import type { Translate } from "./types";

/** Names of the reader settings and their choices. */
const readerLabels = (t: Translate) => ({
  mode: t("阅读模式", "Reading mode"),
  single: t("单页", "Single page"),
  spread: t("双页", "Two pages"),
  scroll: t("纵向滚动", "Vertical scroll"),
  coverSolo: t("封面单独显示", "Show cover on its own"),
  fit: t("适应", "Fit"),
  height: t("适应高度", "Fit height"),
  width: t("适应宽度", "Fit width"),
  direction: t("阅读方向", "Direction"),
  ltr: t("从左到右", "Left to right"),
  rtl: t("从右到左", "Right to left"),
});

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
  const labels = readerLabels(t);
  const panel = useRef<HTMLElement>(null);
  // Opening moves focus into the panel, so the keyboard continues where the eye goes.
  useEffect(() => {
    if (open)
      panel.current?.querySelector<HTMLElement>(".select-control")?.focus();
  }, [open]);
  return (
    <>
      {open && (
        <button
          type="button"
          className="settings-dismiss"
          data-hint=""
          aria-label={t("关闭阅读设置", "Close reading settings")}
          onClick={close}
        />
      )}
      <section
        ref={panel}
        hidden={!open}
        className="reader-settings glass"
        aria-label={t("阅读设置", "Reading settings")}
      >
        <header className="reader-settings-title">
          <h2>{t("阅读设置", "Reading settings")}</h2>
          <button
            type="button"
            className="icon-button close-button"
            aria-label={t("关闭", "Close")}
            onClick={close}
          >
            <X size={16} />
          </button>
        </header>
        <label>
          {labels.mode}
          <Select
            aria-label={labels.mode}
            value={mode}
            onChange={(event) =>
              change({ mode: event.target.value as ReadingMode })
            }
          >
            <option value="single">{labels.single}</option>
            <option value="spread">{labels.spread}</option>
            <option value="scroll">{labels.scroll}</option>
          </Select>
        </label>
        {mode === "spread" && (
          <label className="checkbox">
            <input
              type="checkbox"
              checked={coverSolo}
              onChange={(event) => change({ coverSolo: event.target.checked })}
            />
            {labels.coverSolo}
          </label>
        )}
        <label>
          {labels.fit}
          <Select
            value={fit}
            onChange={(event) =>
              change({ fit: event.target.value as ReadingFit })
            }
          >
            <option value="height">{labels.height}</option>
            <option value="width">{labels.width}</option>
          </Select>
        </label>
        <label>
          {labels.direction}
          <Select
            value={direction}
            aria-label={labels.direction}
            disabled={mode === "scroll"}
            onChange={(event) =>
              change({ direction: event.target.value as ReadingDirection })
            }
          >
            <option value="ltr">{labels.ltr}</option>
            <option value="rtl">{labels.rtl}</option>
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
