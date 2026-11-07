import { createContext, useContext } from "react";
import { ArrowLeft } from "lucide-react";
import { storageKey } from "./device";
import type { Translate } from "./types";

export const validRoute = (route: string) =>
  /^(?:|#(?:(?:book|read)\/[a-zA-Z0-9-]+(?:\/in\/[a-zA-Z0-9-]+)?(?:\/page\/\d+)?|collection\/[a-zA-Z0-9-]+|collections|activity|settings|downloads))$/.test(
    route,
  );
export function parentRoute(route: string): string {
  const [page, id, kind, target] = route.slice(1).split("/");
  const collection = kind === "in" ? target : undefined;
  if (page === "read")
    return `#book/${id}${collection ? `/in/${collection}` : ""}`;
  if (page === "book") return collection ? `#collection/${collection}` : "";
  if (page === "collection") return "#collections";
  return "";
}

export function defaultTrail(route: string): string[] {
  return route ? [...defaultTrail(parentRoute(route)), route] : [""];
}

export function advanceTrail(trail: string[], next: string): string[] {
  const index = trail.lastIndexOf(next);
  if (index >= 0) return trail.slice(0, index + 1);
  const previous = trail.at(-1) || "";
  if (previous.startsWith("#read/")) {
    if (next === previous.replace("#read/", "#book/"))
      return [...trail.slice(0, -1), next];
    if (next.startsWith("#read/")) {
      const parents = trail.slice(0, -1);
      if (parents.at(-1) === previous.replace("#read/", "#book/"))
        parents[parents.length - 1] = next.replace("#read/", "#book/");
      return [...parents, next];
    }
  }
  return [...trail.slice(-62), next];
}

const trailKey = storageKey("navigation");
export function restoreTrail(route: string): string[] {
  try {
    const stored: unknown = JSON.parse(
      sessionStorage.getItem(trailKey) || "null",
    );
    if (
      Array.isArray(stored) &&
      stored.length > 0 &&
      stored.length <= 64 &&
      stored.every((item) => typeof item === "string" && validRoute(item)) &&
      stored.at(-1) === route
    )
      return stored;
  } catch {
    /* Storage may be unavailable. Direct links still have a parent. */
  }
  return defaultTrail(route);
}

export function rememberTrail(trail: string[]) {
  try {
    sessionStorage.setItem(trailKey, JSON.stringify(trail));
  } catch {
    /* Optional browser persistence. */
  }
}

function routeLabel(route: string, t: Translate): string {
  const names: Record<string, [string, string]> = {
    "": ["书库", "Library"],
    settings: ["设置", "Settings"],
    downloads: ["下载", "Downloads"],
    collections: ["全部合集", "All collections"],
    collection: ["合集", "Collection"],
    activity: ["阅读记录", "Reading activity"],
    book: ["书籍详情", "Book details"],
    read: ["阅读器", "Reader"],
  };
  return t(
    ...(names[route.replace(/^#/, "").split("/")[0] || ""] || names[""]!),
  );
}

export const NavigationContext = createContext<{
  target: string;
  navigate: (route: string) => void;
} | null>(null);

/** Returns to the previous route on the navigation trail; the App always provides the context. */
export function BackButton({ t }: { t: Translate }) {
  const navigation = useContext(NavigationContext);
  if (!navigation) return null;
  return (
    <button
      type="button"
      className="text-button back-link"
      onClick={() => navigation.navigate(navigation.target)}
    >
      <ArrowLeft size={18} />
      {routeLabel(navigation.target, t)}
    </button>
  );
}
