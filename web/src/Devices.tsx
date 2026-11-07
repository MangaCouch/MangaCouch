import { useState } from "react";
import { Monitor, Smartphone, LoaderCircle } from "lucide-react";
import { errorMessage, request } from "./api";
import {
  loadCredentials,
  revokeCredential,
  type Credential,
} from "./credentialList";
import { Pill } from "./Pills";
import { usePolling } from "./polling";
import type { Translate } from "./types";

/** Signed-out or expired sessions stay listed this long so a sign-out can be confirmed. */
const RECENT_SESSION_SECONDS = 2 * 24 * 3600;
export function deviceLabel() {
  const ua = navigator.userAgent;
  const platform =
    /iPad/.test(ua) || (/Macintosh/.test(ua) && navigator.maxTouchPoints > 1)
      ? "iPad"
      : /iPhone/.test(ua)
        ? "iPhone"
        : /Android/.test(ua)
          ? "Android"
          : /Macintosh|Mac OS/.test(ua)
            ? "Mac"
            : /Windows/.test(ua)
              ? "Windows"
              : /Linux/.test(ua)
                ? "Linux"
                : "Browser";
  return `${platform} / ${/Edg/.test(ua) ? "Edge" : /Chrome|CriOS/.test(ua) ? "Chrome" : /Firefox|FxiOS/.test(ua) ? "Firefox" : /Safari/.test(ua) ? "Safari" : "Web"}`;
}
export function Devices({ t, logout }: { t: Translate; logout: () => void }) {
  const [items, setItems] = useState<Credential[]>([]),
    [me, setMe] = useState(""),
    [busy, setBusy] = useState(""),
    [error, setError] = useState<unknown>(null);
  const { error: loadError, refresh } = usePolling(async (signal) => {
    const [user, credentials] = await Promise.all([
      request<{ id: string }>("/api/auth/me", { signal }),
      loadCredentials(signal),
    ]);
    if (signal.aborted) return;
    setMe(user.id);
    const recent = Date.now() / 1000 - RECENT_SESSION_SECONDS;
    setItems(
      credentials.filter(
        (item) =>
          item.kind === "session" &&
          (!item.revoked_at || item.revoked_at > recent) &&
          (!item.expires_at || item.expires_at > recent),
      ),
    );
  }, 30_000);
  async function revoke(id: string) {
    setBusy(id);
    setError(null);
    try {
      await revokeCredential(id);
    } catch (failure) {
      setError(failure);
    } finally {
      setBusy("");
    }
    await refresh();
  }
  return (
    <section>
      {!!(error ?? loadError) && (
        <p className="error-text">{errorMessage(error ?? loadError, t)}</p>
      )}
      <ul className="device-list">
        {items.map((item) => {
          const active =
            !item.revoked_at &&
            (!item.expires_at || item.expires_at > Date.now() / 1000);
          return (
            <li key={item.id}>
              {/iPhone|iPad|Android/.test(item.label) ? (
                <Smartphone size={20} />
              ) : (
                <Monitor size={20} />
              )}
              <div>
                <strong>{item.label}</strong>
                {item.id === me && <Pill>{t("当前设备", "This device")}</Pill>}
                <p className="small muted">
                  {new Date(item.created_at * 1000).toLocaleString()}
                </p>
                {!active && (
                  <Pill>{t("已退出或过期", "Signed out or expired")}</Pill>
                )}
              </div>
              {active && (
                <button
                  disabled={!!busy}
                  onClick={() =>
                    item.id === me ? logout() : void revoke(item.id)
                  }
                >
                  {busy === item.id && (
                    <LoaderCircle size={16} className="spinner" />
                  )}
                  {t("退出设备", "Sign out")}
                </button>
              )}
            </li>
          );
        })}
      </ul>
    </section>
  );
}
