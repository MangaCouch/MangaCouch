import { useEffect, useRef, useState } from "react";
import { Select } from "./Controls";
import { errorMessage, post, UserError } from "./api";
import {
  loadCredentials,
  revokeCredential,
  type Credential,
} from "./credentialList";
import { Modal } from "./Modal";
import { usePolling } from "./polling";
import type { Translate } from "./types";

type Kind = "readonly" | "downloads" | "management";
type Created = { id: string; kind: Kind; key: string };
const length = (value: string) => Array.from(value).length;

export function Credentials({
  t,
  guard,
}: {
  t: Translate;
  guard: { current: (() => Promise<boolean>) | null };
}) {
  const [keys, setKeys] = useState<Credential[] | null>(null),
    [label, setLabel] = useState(""),
    [kind, setKind] = useState<Kind>("readonly"),
    [created, setCreated] = useState<Created | null>(null),
    [copied, setCopied] = useState(false),
    [confirm, setConfirm] = useState<Credential | null>(null),
    [showRevoked, setShowRevoked] = useState(false),
    [busy, setBusy] = useState(false),
    [error, setError] = useState<unknown>(null),
    [notice, setNotice] = useState("");
  const working = useRef(false); // blocks a second submit before the re-render disables it
  const { error: loadError, refresh } = usePolling(async (signal) => {
    const items = await loadCredentials(signal);
    if (signal.aborted) return;
    setKeys(
      items
        .filter((item) => item.kind !== "session")
        .sort(
          (a, b) => b.created_at - a.created_at || b.id.localeCompare(a.id),
        ),
    );
  }, 15_000);
  // The full key is shown once; leaving the page would lose it.
  useEffect(() => {
    guard.current = async () => !created;
    return () => {
      guard.current = null;
    };
  }, [guard, created]);
  const kinds: Record<Kind, [string, string]> = {
    readonly: [
      t("只读", "Read-only"),
      t(
        "可浏览、阅读和下载；不能修改书库或保存阅读进度。",
        "Can browse, read and download; cannot change the library or save reading progress.",
      ),
    ],
    downloads: [
      t(
        "浏览器扩展：只能添加下载",
        "Browser extension: can only add downloads",
      ),
      t(
        "供浏览器扩展使用：只能提交下载链接并查看这些任务的进度，不能浏览或修改书库。",
        "For the browser extension: can only submit download links and check their progress; cannot browse or change the library.",
      ),
    ],
    management: [
      t("完全", "Full access"),
      t(
        "具有管理权限，包括修改书库、创建和撤销其他密钥。",
        "Grants management access, including library changes and creating or revoking other keys.",
      ),
    ],
  };
  const kindName = (value: string) => kinds[value as Kind]?.[0] ?? value;
  /** One action at a time; whatever happens, the list is reloaded afterwards. */
  async function act(action: () => Promise<void>) {
    if (working.current) return;
    working.current = true;
    setBusy(true);
    setError(null);
    setNotice("");
    try {
      await action();
    } catch (failure) {
      setError(failure);
    } finally {
      working.current = false;
      setBusy(false);
    }
    await refresh();
  }
  const valid = label.trim().length > 0 && length(label.trim()) <= 128;
  const create = () =>
    act(async () => {
      if (!valid || created) return;
      setCreated(
        await post<Created>("/api/auth/keys", { label: label.trim(), kind }),
      );
      setCopied(false);
      setLabel("");
    });
  const revoke = (item: Credential) =>
    act(async () => {
      await revokeCredential(item.id);
      if (created?.id === item.id) setCreated(null);
      setConfirm(null);
      setNotice(
        t(
          "密钥已撤销，使用它的后续请求将被拒绝。",
          "Key revoked. Future requests using it will be rejected.",
        ),
      );
    });
  async function copy() {
    if (!created) return;
    setError(null);
    try {
      await navigator.clipboard.writeText(created.key);
      setCopied(true);
    } catch {
      setError(
        new UserError(
          t(
            "复制失败，请选中完整密钥手动复制。",
            "Copy failed. Select the full key and copy it manually.",
          ),
        ),
      );
    }
  }
  const shown = error ?? loadError;
  const failure = !!shown && (
    <p role="alert" className="notice error">
      {errorMessage(shown, t)}
    </p>
  );
  const listed = (keys ?? []).filter(
    (item) => showRevoked || item.revoked_at === null,
  );
  return (
    <section className="credentials-page">
      {failure}
      {notice && (
        <p role="status" className="notice">
          {notice}
        </p>
      )}
      <h4>{t("创建 API key", "Create API key")}</h4>
      <form
        onSubmit={(event) => {
          event.preventDefault();
          void create();
        }}
      >
        <label>
          {t("名称", "Name")}
          <input
            autoComplete="off"
            maxLength={256}
            value={label}
            disabled={busy || !!created}
            onChange={(event) => setLabel(event.target.value)}
          />
        </label>
        <label>
          {t("权限", "Permission")}
          <Select
            value={kind}
            disabled={busy || !!created}
            onChange={(event) => setKind(event.target.value as Kind)}
          >
            {(Object.keys(kinds) as Kind[]).map((value) => (
              <option key={value} value={value}>
                {kinds[value][0]}
              </option>
            ))}
          </Select>
        </label>
        <p className="muted small">{kinds[kind][1]}</p>
        <div className="actions">
          <button className="primary" disabled={busy || !valid || !!created}>
            {t("创建密钥", "Create key")}
          </button>
        </div>
      </form>
      {created && (
        <div className="notice key-reveal" role="status">
          <h4>{t("完整密钥只在本次显示", "Full key shown only this time")}</h4>
          <p>
            {kindName(created.kind)}{" "}
            {t(
              "请保存到安全位置；关闭后无法再次读取完整值。",
              "Save it securely; the full value cannot be retrieved after closing.",
            )}
          </p>
          <label>
            {t("完整 API key", "Full API key")}
            <textarea
              readOnly
              autoComplete="off"
              spellCheck={false}
              value={created.key}
            />
          </label>
          <div className="actions">
            <button onClick={() => void copy()}>
              {copied ? t("已复制", "Copied") : t("复制密钥", "Copy key")}
            </button>
            <button onClick={() => setCreated(null)}>
              {t("已保存，关闭显示", "Saved; close display")}
            </button>
          </div>
        </div>
      )}
      <h4>{t("已有 API key", "Existing API keys")}</h4>
      <label className="check-label">
        <input
          type="checkbox"
          checked={showRevoked}
          onChange={(event) => setShowRevoked(event.target.checked)}
        />
        {t("显示已撤销", "Show revoked")}
      </label>
      {!keys && !loadError && <p role="status">{t("正在读取…", "Loading…")}</p>}
      {keys && !listed.length && (
        <p>{t("没有符合条件的密钥。", "No matching keys.")}</p>
      )}
      {listed.map((item) => (
        <article className="credential-item" key={item.id}>
          <div>
            <strong>{item.label}</strong>
            <p>
              {kindName(item.kind)}{" "}
              {item.revoked_at === null
                ? t("有效", "Active")
                : t("已撤销", "Revoked")}
            </p>
            <p className="muted small">
              {t("创建于", "Created")}{" "}
              {new Date(item.created_at * 1000).toLocaleString()}
            </p>
          </div>
          {item.revoked_at === null && (
            <button
              disabled={busy}
              onClick={() => setConfirm(item)}
              aria-label={t(`撤销 ${item.label}`, `Revoke ${item.label}`)}
            >
              {t("撤销", "Revoke")}
            </button>
          )}
        </article>
      ))}
      {confirm && (
        <Modal
          title={t(`撤销“${confirm.label}”？`, `Revoke “${confirm.label}”?`)}
          closeLabel={t("取消", "Cancel")}
          close={() => setConfirm(null)}
          busy={busy}
        >
          {failure}
          <p>
            {t(
              "使用此密钥的客户端将失去访问权限；其他密钥和网页会话不受影响。",
              "Clients using this key will lose access. Other keys and web sessions are unchanged.",
            )}
          </p>
          <div className="actions">
            <button disabled={busy} onClick={() => void revoke(confirm)}>
              {t("确认撤销", "Confirm revocation")}
            </button>
            <button disabled={busy} onClick={() => setConfirm(null)}>
              {t("取消", "Cancel")}
            </button>
          </div>
        </Modal>
      )}
    </section>
  );
}
