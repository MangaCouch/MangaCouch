import { createContext, useContext, type AnchorHTMLAttributes } from "react";
import { ArrowLeft } from "lucide-react";
import { storageKey } from "./device";
import type { Translate } from "./types";

/**
 * Asks the current page whether it may be left (no running work or unsaved changes):
 * true lets it go; false holds it, and text holds it saying why.
 */
export type Guard = (next?: string) => Promise<boolean | string>;
/** Where a page registers its guard; null when nothing holds the page open. */
export type GuardRef = { current: Guard | null };

export const validRoute = (route: string) =>
  /^(?:|#(?:(?:book|read)\/[a-zA-Z0-9-]+(?:\/in\/[a-zA-Z0-9-]+)?(?:\/page\/\d+)?|collection\/[a-zA-Z0-9-]+|collections|activity|settings(?:\/[a-z-]+)?|downloads(?:\/[a-zA-Z0-9-]+)?))$/.test(
    route,
  );
export function parentRoute(route: string): string {
  const [page, id, kind, target] = route.slice(1).split("/");
  const collection = kind === "in" ? target : undefined;
  if (page === "read")
    return `#book/${id}${collection ? `/in/${collection}` : ""}`;
  if (page === "book") return collection ? `#collection/${collection}` : "";
  if (page === "collection") return "#collections";
  if ((page === "downloads" || page === "settings") && id) return `#${page}`;
  return "";
}

/**
 * The page a step back from `from` to the history entry `landing` shows. "Next book"
 * replaces a collection reader in place, so the entry before it is still the first
 * book's details; going back from the book being read returns to its own details there.
 */
export function returnRoute(from: string, landing: string): string {
  const reader =
    /^#read\/([a-zA-Z0-9-]+)\/in\/([a-zA-Z0-9-]+)(?:\/page\/\d+)?$/.exec(from);
  const detail = /^#book\/([a-zA-Z0-9-]+)\/in\/([a-zA-Z0-9-]+)$/.exec(landing);
  return reader && detail && reader[2] === detail[2] && reader[1] !== detail[1]
    ? `#book/${reader[1]}/in/${reader[2]}`
    : landing;
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
  /** Where the page's own back button leads. */
  target: string;
  /** Goes back to `route` (the target or one of its ancestors), reusing history entries. */
  navigate: (route: string) => void;
  /** Swaps the current page for `route` without adding a history entry. */
  replace: (route: string) => void;
} | null>(null);

/** Returns to the previous route on the navigation trail; the App always provides the context. */
export function BackButton({ t }: { t: Translate }) {
  const navigation = useContext(NavigationContext);
  if (!navigation) return null;
  return (
    <RouteLink
      className="text-button back-link"
      route={navigation.target}
      navigate={navigation.navigate}
    >
      <ArrowLeft size={18} />
      {routeLabel(navigation.target, t)}
    </RouteLink>
  );
}

/** Let the browser handle modified clicks, middle-clicks and link context menus. */
export function RouteLink({
  route,
  navigate,
  disabled = false,
  onClick,
  className = "",
  ...props
}: AnchorHTMLAttributes<HTMLAnchorElement> & {
  route: string;
  navigate: (route: string) => void;
  disabled?: boolean;
}) {
  return (
    <a
      {...props}
      data-route-link=""
      className={className}
      href={
        disabled
          ? undefined
          : !route
            ? location.pathname
            : route.startsWith("#")
              ? route
              : `#${route}`
      }
      aria-disabled={disabled || undefined}
      onClick={(event) => {
        if (disabled) {
          event.preventDefault();
          return;
        }
        if (
          event.button !== 0 ||
          event.ctrlKey ||
          event.metaKey ||
          event.shiftKey ||
          event.altKey
        )
          return;
        event.preventDefault();
        onClick?.(event);
        navigate(route);
      }}
    />
  );
}
