import type { Source } from "./types";

export const jobId = () => {
  const bytes = crypto.getRandomValues(new Uint8Array(16));
  bytes[6] = (bytes[6]! & 15) | 64;
  bytes[8] = (bytes[8]! & 63) | 128;
  const hex = Array.from(bytes, (b) => b.toString(16).padStart(2, "0")).join(
    "",
  );
  return `${hex.slice(0, 8)}-${hex.slice(8, 12)}-${hex.slice(12, 16)}-${hex.slice(16, 20)}-${hex.slice(20)}`;
};
/** Canonical gallery URL for a source identity (E-Hentai/ExHentai include the token). */
export const sourceURL = (source: Source | null | undefined): string =>
  source?.site === "nhentai"
    ? `https://nhentai.net/g/${source.gallery_id}/`
    : source?.site === "ehentai" || source?.site === "exhentai"
      ? `https://${source.site === "ehentai" ? "e-hentai.org" : "exhentai.org"}/g/${source.gallery_id}/${source.gallery_token}/`
      : "";
/** A source site's name; E-Hentai and ExHentai list the same galleries, so they share one. */
export const siteLabel = (site: string) =>
  site === "nhentai" ? "nHentai" : "E(x)Hentai";
