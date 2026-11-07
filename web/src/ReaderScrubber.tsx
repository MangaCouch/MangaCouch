import { Segmented } from "./Controls";
import { AUTOPLAY_SECONDS } from "./device";
import type { Translate } from "./types";

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
  count,
  move,
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
  count: number;
  move: (page: number) => void;
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
          aria-label={t("阅读进度", "Reading position")}
          min={0}
          max={count - 1}
          value={page}
          onChange={(event) => move(Number(event.target.value))}
        />
        <div className="scrubber-controls">
          <span>
            {page + 1} / {count}
          </span>
          <Segmented
            label={t("自动播放", "Autoplay")}
            value={playing ? String(seconds) : "off"}
            options={[
              { value: "off", label: t("关", "Off") },
              ...AUTOPLAY_SECONDS.map((value) => ({
                value: String(value),
                label: `${value}s`,
                disabled: autoplayDisabled,
              })),
            ]}
            change={(value) => play(value === "off" ? null : Number(value))}
          />
        </div>
      </div>
    </div>
  );
}
