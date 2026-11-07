import { Star, X } from "lucide-react";
import type { Translate } from "./types";

export function Rating({
  value,
  change,
  t,
  label = t("我的评分", "My rating"),
}: {
  value: number | null;
  change: (value: number | null) => void;
  t: Translate;
  label?: string;
}) {
  const shown = value ?? 0;
  return (
    <div className="rating">
      <span>{label}</span>
      <div className="rating-stars">
        {[1, 2, 3, 4, 5].map((star) => (
          <span className="rating-star" key={star} aria-hidden="true">
            <Star size={25} />
            <span
              className="star-fill"
              style={{
                width: `${Math.max(0, Math.min(1, shown - star + 1)) * 100}%`,
              }}
            >
              <Star size={25} fill="currentColor" />
            </span>
          </span>
        ))}
        <input
          className="rating-input"
          type="range"
          min={0}
          max={5}
          step={0.5}
          value={shown}
          aria-label={label}
          aria-valuetext={
            value === null
              ? t("未评分", "Unrated")
              : t(`${value} 星`, `${value} stars`)
          }
          onChange={(event) => change(Number(event.target.value) || null)}
        />
      </div>
      <span className="rating-value">
        {value === null ? t("未评分", "Unrated") : value}
      </span>
      {value !== null && (
        <button
          type="button"
          className="icon-button"
          aria-label={t("清除评分", "Clear rating")}
          onClick={() => change(null)}
        >
          <X size={16} />
        </button>
      )}
    </div>
  );
}
