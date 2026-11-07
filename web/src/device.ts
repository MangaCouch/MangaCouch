/** The autoplay intervals offered by the reader and the shared defaults. */
export const AUTOPLAY_SECONDS = [3, 5, 8, 10, 12, 15, 20, 30] as const;
export type AutoplaySeconds = (typeof AUTOPLAY_SECONDS)[number];
export type ReadingFit = "height" | "width";
export type ReadingDirection = "ltr" | "rtl";
export type DeviceDefaults = {
  reading_mode: "single" | "spread" | "scroll";
  fit: ReadingFit;
  direction: ReadingDirection;
  cover_solo: boolean;
  autoplay_seconds: AutoplaySeconds;
};
export const defaultDevice: DeviceDefaults = {
  reading_mode: "single",
  fit: "height",
  direction: "ltr",
  cover_solo: true,
  autoplay_seconds: 5,
};
/** localStorage names of the per-device reader settings. */
export const deviceKeys = {
  reading_mode: "reading-mode",
  fit: "fit",
  direction: "direction",
  cover_solo: "cover-solo",
  autoplay_seconds: "autoplay-seconds",
} as const;
const allowed = {
  reading_mode: ["single", "spread", "scroll"],
  fit: ["height", "width"],
  direction: ["ltr", "rtl"],
  cover_solo: ["true", "false"],
  autoplay_seconds: AUTOPLAY_SECONDS.map(String),
};
/** Browser storage key for a per-device setting; the single namespace for localStorage and sessionStorage. */
export const storageKey = (name: string) => `mangacouch:${name}`;
export function preference(key: string, fallback: string): string {
  try {
    return localStorage.getItem(storageKey(key)) ?? fallback;
  } catch {
    return fallback;
  }
}
export function remember(key: string, value: string): boolean {
  try {
    localStorage.setItem(storageKey(key), value);
    return true;
  } catch {
    return false;
  }
}
function deviceValues(defaults: DeviceDefaults, initialize: boolean) {
  const values = { ...defaults };
  for (const key of Object.keys(deviceKeys) as (keyof DeviceDefaults)[]) {
    let raw = preference(deviceKeys[key], "");
    if (!allowed[key].includes(raw)) {
      raw = String(defaults[key]);
      if (initialize) remember(deviceKeys[key], raw);
    }
    Object.assign(values, {
      [key]:
        key === "cover_solo"
          ? raw === "true"
          : key === "autoplay_seconds"
            ? Number(raw)
            : raw,
    });
  }
  return values;
}
export const readDevice = (defaults = defaultDevice) =>
  deviceValues(defaults, false);
/** Reads this device's settings, writing the given defaults for any key that is missing or invalid. */
export const initializeDevice = (defaults: DeviceDefaults) =>
  deviceValues(defaults, true);
