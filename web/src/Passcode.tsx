import { useState } from "react";
import { errorMessage, post } from "./api";
import type { Translate } from "./types";

const length = (value: string) => Array.from(value).length;

/** Changes the library passcode; the server signs out every web session afterwards. */
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
          await post("/api/auth/passcode", { replacement });
          setReplacement("");
          setRepeat("");
          lock(true);
        } catch (failure) {
          setError(failure);
        } finally {
          setBusy(false);
        }
      }}
    >
      <p className="muted small">
        {t("修改后需重新登录。", "Sign in again after changing the passcode.")}
      </p>
      {!!error && (
        <p role="alert" className="notice error">
          {errorMessage(error, t)}
        </p>
      )}
      {field(
        t("新口令（8 至 256 个字符）", "New passcode (8 to 256 characters)"),
        replacement,
        setReplacement,
      )}
      {field(t("再次输入新口令", "Repeat new passcode"), repeat, setRepeat)}
      {repeat && repeat !== replacement && (
        <p className="error-text">
          {t("两次新口令不一致。", "New passcodes do not match.")}
        </p>
      )}
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
