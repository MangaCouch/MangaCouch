export type ReadingFit = "height" | "width";
export type ReadingDirection = "ltr" | "rtl";
/** Reader settings; each device keeps its own. */
export type DeviceSettings = {
  reading_mode: "single" | "spread" | "scroll";
  fit: ReadingFit;
  direction: ReadingDirection;
  cover_solo: boolean;
};
/** What a device that never changed a reader setting uses. */
export const defaultDevice: DeviceSettings = {
  reading_mode: "single",
  fit: "height",
  direction: "ltr",
  cover_solo: true,
};
/** localStorage names of the per-device reader settings. */
const deviceKeys = {
  reading_mode: "reading-mode",
  fit: "fit",
  direction: "direction",
  cover_solo: "cover-solo",
} as const;
const allowed = {
  reading_mode: ["single", "spread", "scroll"],
  fit: ["height", "width"],
  direction: ["ltr", "rtl"],
  cover_solo: ["true", "false"],
};
/** Browser storage key for a per-device setting; the single namespace for localStorage and sessionStorage. */
export const storageKey = (name: string) => `mangacouch:${name}`;
/** The browser's language on a first visit: Chinese for any Chinese locale, else English. */
export function browserLanguage(): "zh" | "en" {
  try {
    const wanted = navigator.languages?.length
      ? navigator.languages
      : [navigator.language];
    return wanted.some((tag) => /^zh\b/i.test(tag ?? "")) ? "zh" : "en";
  } catch {
    return "zh";
  }
}

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
/** This device's reader settings; a missing or invalid one takes its default. */
export function readDevice(): DeviceSettings {
  const values = { ...defaultDevice };
  for (const key of Object.keys(deviceKeys) as (keyof typeof deviceKeys)[]) {
    let raw = preference(deviceKeys[key], "");
    if (!allowed[key].includes(raw)) raw = String(defaultDevice[key]);
    Object.assign(values, {
      [key]: key === "cover_solo" ? raw === "true" : raw,
    });
  }
  return values;
}
