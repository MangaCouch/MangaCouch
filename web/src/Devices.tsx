import { useState } from "react";
import { Globe, Monitor, Smartphone, Tablet } from "lucide-react";
import { useAction } from "./action";
import { errorMessage, request } from "./api";
import { SkeletonRows } from "./Controls";
import { dateTime } from "./format";
import { Modal } from "./Modal";
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
          ? // Android phones say "Mobile"; tablets leave it out.
            /Mobile/.test(ua)
            ? "Android"
            : "Android tablet"
          : /Macintosh|Mac OS/.test(ua)
            ? "Mac"
            : /Windows/.test(ua)
              ? "Windows"
              : /Linux/.test(ua)
                ? "Linux"
                : "Browser";
  return `${platform} / ${/Edg/.test(ua) ? "Edge" : /Chrome|CriOS/.test(ua) ? "Chrome" : /Firefox|FxiOS/.test(ua) ? "Firefox" : /Safari/.test(ua) ? "Safari" : "Web"}`;
}

type DeviceKind = "phone" | "tablet" | "computer" | "unknown";

/**
 * The kind of device a session label names. Sign-in stores `deviceLabel()`, read from the
 * browser's user agent ("iPhone / Safari"); older or hand-made labels are matched by word.
 */
export function deviceKind(label: string): DeviceKind {
  const text = label.toLowerCase();
  if (/ipad|tablet|平板/.test(text)) return "tablet";
  if (/iphone|android|phone|手机/.test(text)) return "phone";
  if (/mac|windows|linux|desktop|laptop|computer|电脑/.test(text))
    return "computer";
  return "unknown";
}

/** "iPhone / Safari" read as "Safari on iPhone"; any other label is shown as stored. */
export function readableDevice(label: string, t: Translate) {
  const match = /^(.+?) \/ (\S+)$/.exec(label);
  if (!match) return label;
  const platform = match[1]!,
    browser = match[2]!;
  const system =
    platform === "Android tablet"
      ? t("安卓平板", "Android tablet")
      : platform === "Browser"
        ? t("未知系统", "unknown system")
        : platform;
  const app = browser === "Web" ? t("浏览器", "a browser") : browser;
  return t(`${system} 上的 ${app}`, `${app} on ${system}`);
}

const kindIcons = {
  phone: Smartphone,
  tablet: Tablet,
  computer: Monitor,
  unknown: Globe,
};
const kindName = (kind: DeviceKind, t: Translate) =>
  ({
    phone: t("手机", "Phone"),
    tablet: t("平板", "Tablet"),
    computer: t("电脑", "Computer"),
    unknown: t("未知设备", "Unknown device"),
  })[kind];
export function Devices({ t, logout }: { t: Translate; logout: () => void }) {
  const [items, setItems] = useState<Credential[] | null>(null),
    [me, setMe] = useState(""),
    [confirm, setConfirm] = useState<Credential | null>(null),
    [notice, setNotice] = useState("");
  const action = useAction();
  const { busy, error } = action;
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
  async function revoke(item: Credential) {
    setNotice("");
    const done = await action.run(() => revokeCredential(item.id));
    if (done) {
      setConfirm(null);
      const name = readableDevice(item.label, t);
      setNotice(t(`已退出“${name}”。`, `Signed out “${name}”.`));
    }
    await refresh();
  }
  const current = confirm?.id === me;
  const confirmName = confirm ? readableDevice(confirm.label, t) : "";
  const failure = !!(error ?? loadError) && (
    <p className="notice error" role="alert">
      {errorMessage(error ?? loadError, t)}
    </p>
  );
  return (
    <section>
      {!confirm && failure}
      {notice && (
        <p className="notice" role="status">
          {notice}
        </p>
      )}
      {!items && !loadError && (
        <SkeletonRows label={t("正在读取设备…", "Loading devices…")} />
      )}
      {items?.length === 0 && (
        <p className="muted">
          {t("没有已登录的网页设备。", "No signed-in web devices.")}
        </p>
      )}
      <ul className="device-list">
        {items?.map((item) => {
          const active =
            !item.revoked_at &&
            (!item.expires_at || item.expires_at > Date.now() / 1000);
          const kind = deviceKind(item.label);
          const Icon = kindIcons[kind];
          return (
            <li key={item.id} data-device-kind={kind}>
              <Icon size={20} aria-hidden="true" style={{ flexShrink: 0 }} />
              <div>
                <strong>{readableDevice(item.label, t)}</strong>
                {item.id === me && <Pill>{t("当前设备", "This device")}</Pill>}
                <p className="small muted">
                  {kindName(kind, t)} · {t("登录于", "Signed in")}{" "}
                  {dateTime(item.created_at, t("zh-CN", "en-US"))}
                </p>
                {!active && (
                  <Pill>{t("已退出或过期", "Signed out or expired")}</Pill>
                )}
              </div>
              {active && (
                <button
                  disabled={busy}
                  onClick={() => {
                    action.setError(null);
                    setConfirm(item);
                  }}
                >
                  {t("退出设备", "Sign out")}
                </button>
              )}
            </li>
          );
        })}
      </ul>
      {confirm && (
        <Modal
          title={
            current
              ? t("退出当前设备？", "Sign out this device?")
              : t(`退出“${confirmName}”？`, `Sign out “${confirmName}”?`)
          }
          closeLabel={t("取消", "Cancel")}
          close={() => setConfirm(null)}
          busy={busy}
        >
          {failure}
          <p>
            {current
              ? t(
                  "你正在使用这台设备；退出后需要重新输入书库口令才能继续使用。其他设备和 API 密钥不受影响。",
                  "You are using this device. After signing out, it must enter the library passcode again. Other devices and API keys are unchanged.",
                )
              : t(
                  "该设备需要重新输入书库口令才能继续使用；其他设备和 API 密钥不受影响。",
                  "That device must enter the library passcode again. Other devices and API keys are unchanged.",
                )}
          </p>
          <div className="actions modal-actions">
            <button disabled={busy} onClick={() => setConfirm(null)}>
              {t("取消", "Cancel")}
            </button>
            <button
              className="danger"
              disabled={busy}
              onClick={() => {
                if (current) logout();
                else void revoke(confirm);
              }}
            >
              {t("退出设备", "Sign out")}
            </button>
          </div>
        </Modal>
      )}
    </section>
  );
}
