import { useState } from "react";
import { ArrowRight } from "lucide-react";
import { deviceLabel } from "./Devices";
import { errorMessage, post } from "./api";
import type { Translate } from "./types";

/** Passcode form; `notice` explains why the session ended (shown inline, not as a toast). */
export function Login({
  t,
  entered,
  notice,
}: {
  t: Translate;
  entered: () => void;
  notice: string;
}) {
  const [passcode, setPasscode] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<unknown>(null);
  return (
    <main className="login-wrap">
      <section className="login panel">
        <div className="brand-mark">
          <img src="/assets/icon.svg" width={40} height={40} alt="" />
        </div>
        <span className="eyebrow">MANGACOUCH</span>
        <h1>
          {t("留一点时间，\n给喜欢的故事。", "Make time for\na good story.")}
        </h1>
        <form
          onSubmit={(event) => {
            event.preventDefault();
            if (busy) return;
            setBusy(true);
            setError(null);
            void post("/api/auth/login", { passcode, label: deviceLabel() })
              .then(() => {
                setPasscode("");
                entered();
              })
              .catch(setError)
              .finally(() => setBusy(false));
          }}
        >
          <label>
            {t("书库口令", "Library passcode")}
            <input
              type="password"
              autoComplete="current-password"
              required
              maxLength={512}
              value={passcode}
              onChange={(event) => setPasscode(event.target.value)}
            />
          </label>
          <button type="submit" className="primary" disabled={busy}>
            {busy
              ? t("正在登录…", "Signing in…")
              : t("打开书库", "Open library")}
            <ArrowRight size={18} />
          </button>
        </form>
        {!!error && (
          <p role="alert" className="error-text">
            {errorMessage(error, t)}
          </p>
        )}
        {notice && (
          <p role="status" className="muted small">
            {notice}
          </p>
        )}
      </section>
    </main>
  );
}
