import { Timer } from "lucide-react";
import { Segmented } from "./Controls";
import type { Translate } from "./types";

/** Autoplay intervals available on the scrubber. */
const QUICK_SECONDS = [5, 10, 15] as const;

/**
 * Bottom scrubber with the page slider and the autoplay control. While the reader's
 * tools are hidden it is inert (no pointer, touch or focus), and a separate,
 * transparent reveal strip brings the tools back instead of operating the slider.
 */
export function ReaderScrubber({
  hidden,
  toolsVisible,
  reveal,
  page,
  displayed,
  count,
  move,
  direction,
  arrow,
  scrub,
  playing,
  seconds,
  autoplayDisabled,
  play,
  t,
}: {
  hidden: boolean;
  toolsVisible: boolean;
  reveal: () => void;
  page: number;
  /** The pages on screen; a spread shows as "2+3 / 27". */
  displayed: number[];
  count: number;
  move: (page: number) => void;
  direction: "ltr" | "rtl";
  arrow: (key: string, event: KeyboardEvent) => void;
  /** Whether the slider is being dragged with a pointer. */
  scrub: (active: boolean) => void;
  playing: boolean;
  seconds: number;
  autoplayDisabled: boolean;
  /** Starts autoplay at the given interval, or stops it with null. */
  play: (seconds: number | null) => void;
  t: Translate;
}) {
  return (
    <div className="reader-bottom-zone" hidden={hidden}>
      {!toolsVisible && (
        <button
          type="button"
          className="reader-reveal"
          data-hint=""
          aria-label={t("显示阅读控件", "Show reading controls")}
          onPointerEnter={(event) => {
            if (event.pointerType === "mouse") reveal();
          }}
          onClick={reveal}
        />
      )}
      <div
        className="reader-scrubber glass"
        {...(!toolsVisible
          ? { inert: true, "aria-hidden": true as const }
          : {})}
      >
        <input
          type="range"
          dir={direction}
          onKeyDown={(event) => {
            if (event.key.startsWith("Arrow")) {
              event.preventDefault();
              arrow(event.key, event.nativeEvent);
            }
          }}
          aria-label={t("阅读位置", "Reading position")}
          min={0}
          max={count - 1}
          value={page}
          onChange={(event) => move(Number(event.target.value))}
          onPointerDown={() => {
            scrub(true);
            // The release can land outside the slider, so listen on the window.
            const end = () => {
              scrub(false);
              window.removeEventListener("pointerup", end);
              window.removeEventListener("pointercancel", end);
            };
            window.addEventListener("pointerup", end);
            window.addEventListener("pointercancel", end);
          }}
        />
        <div className="scrubber-controls">
          <span>
            {displayed.map((index) => index + 1).join("+")} / {count}
          </span>
          <div className="heading-with-info">
            <Timer size={16} aria-hidden="true" />
            <Segmented
              label={t("自动播放", "Autoplay")}
              value={playing ? String(seconds) : ""}
              options={QUICK_SECONDS.map((value) => ({
                value: String(value),
                label: `${value}s`,
                disabled: autoplayDisabled,
              }))}
              change={(value) =>
                play(
                  playing && Number(value) === seconds ? null : Number(value),
                )
              }
            />
          </div>
        </div>
      </div>
    </div>
  );
}
