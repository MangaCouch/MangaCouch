export function bytes(value: number): string {
  if (value < 1024) return `${Math.round(value)} B`;
  const units = ["KiB", "MiB", "GiB", "TiB"];
  const exponent = Math.min(4, Math.floor(Math.log(value) / Math.log(1024)));
  return `${(value / 1024 ** exponent).toFixed(1)} ${units[exponent - 1]}`;
}

export const exactBytes = (value: number) =>
  `${bytes(value)} (${value.toLocaleString()} B)`;

/** Formats a timestamp (ISO 8601 string or Unix seconds) for the given locale; unparseable input is shown as-is. */
export function dateTime(
  value: string | number,
  locale: string,
  options: Intl.DateTimeFormatOptions = {
    dateStyle: "medium",
    timeStyle: "short",
  },
): string {
  const date = new Date(typeof value === "number" ? value * 1000 : value);
  return Number.isNaN(date.getTime())
    ? String(value)
    : new Intl.DateTimeFormat(locale, options).format(date);
}
