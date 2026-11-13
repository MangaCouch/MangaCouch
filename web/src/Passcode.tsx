import { useState } from "react";
import { uncertainWrite, errorMessage, post } from "./api";
import type { Translate } from "./types";

const length = (value: string) => Array.from(value).length;

/**
 * Changes the library passcode; the server signs out every web session afterwards and, when
 * asked, revokes every API key too.
 */
export function PasscodeChange({
  t,
  lock,
}: {
  t: Translate;
  lock: (confirmed: boolean) => void;
}) {
  const [replacement, setReplacement] = useState(""),
    [repeat, setRepeat] = useState(""),
    [acknowledged, setAcknowledged] = useState(false),
    [revokeKeys, setRevokeKeys] = useState(false),
    [busy, setBusy] = useState(false),
    [error, setError] = useState<unknown>(null);
  const valid =
    length(replacement) >= 8 &&
    length(replacement) <= 256 &&
    replacement === repeat &&
    acknowledged;
  const field = (text: string, value: string, set: (v: string) => void) => (
    <label>
      {text}
      <input
        type="password"
        autoComplete="new-password"
        minLength={8}
        maxLength={512}
        value={value}
        disabled={busy}
        onChange={(event) => {
          set(event.target.value);
          setAcknowledged(false);
        }}
      />
    </label>
  );
  return (
    <form
      className="passcode-change"
      onSubmit={async (event) => {
        event.preventDefault();
        if (!valid || busy) return;
        setBusy(true);
        setError(null);
        try {
          await post("/api/auth/passcode", {
            replacement,
            revoke_keys: revokeKeys,
          });
          setReplacement("");
          setRepeat("");
          setRevokeKeys(false);
          lock(true);
        } catch (failure) {
          setError(failure);
        } finally {
          setBusy(false);
        }
      }}
    >
      {!!error && (
        <p role="alert" className="notice error">
          {errorMessage(error, t)}
          {uncertainWrite(error) &&
            t(
              " 无法确认口令是否已修改：之后需要登录时，请先试新口令，不行再用原口令。",
              " Whether the passcode changed is unknown: when asked to sign in, try the new passcode first, then the old one.",
            )}
        </p>
      )}
      {field(t("新口令", "New passcode"), replacement, setReplacement)}
      {field(t("再次输入新口令", "Repeat new passcode"), repeat, setRepeat)}
      {replacement &&
        (length(replacement) < 8 || length(replacement) > 256) && (
          <p className="error-text">
            {t(
              "口令需为 8 至 256 个字符。",
              "Use 8 to 256 characters for the passcode.",
            )}
          </p>
        )}
      {repeat && repeat !== replacement && (
        <p className="error-text">
          {t("两次新口令不一致。", "New passcodes do not match.")}
        </p>
      )}
      <label className="check-label">
        <input
          type="checkbox"
          disabled={busy}
          checked={revokeKeys}
          onChange={(event) => setRevokeKeys(event.target.checked)}
        />
        {t(
          "同时撤销所有 API 密钥（浏览器扩展等客户端需要换用新密钥）",
          "Also revoke all API keys (the browser extension and other clients then need new keys)",
        )}
      </label>
      <label className="check-label">
        <input
          type="checkbox"
          disabled={busy}
          checked={acknowledged}
          onChange={(event) => setAcknowledged(event.target.checked)}
        />
        {t(
          "我知道所有网页设备将退出，需要用新口令重新登录。",
          "I understand all web devices will sign out and require the new passcode.",
        )}
      </label>
      <div className="actions">
        <button className="primary" disabled={busy || !valid}>
          {t(
            "更改口令并退出网页设备",
            "Change passcode and sign out web devices",
          )}
        </button>
      </div>
    </form>
  );
}
