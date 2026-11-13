import { InfoTip } from "./InfoTip";
import type { GuardRef } from "./navigation";
import { useEffect, useRef, useState } from "react";
import { useAction } from "./action";
import { copyText } from "./clipboard";
import { Select, SkeletonRows } from "./Controls";
import { uncertainWrite, errorMessage, post, UserError } from "./api";
import { dateTime } from "./format";
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
/** The longest key name the server accepts (in characters). */
const NAME_LIMIT = 128;

export function Credentials({ t, guard }: { t: Translate; guard: GuardRef }) {
  const [keys, setKeys] = useState<Credential[] | null>(null),
    [label, setLabel] = useState(""),
    [kind, setKind] = useState<Kind>("readonly"),
    [created, setCreated] = useState<Created | null>(null),
    [copied, setCopied] = useState(false),
    [confirm, setConfirm] = useState<Credential | null>(null),
    [showRevoked, setShowRevoked] = useState(false),
    [notice, setNotice] = useState("");
  const action = useAction();
  const { busy, error, setError } = action;
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
  const root = useRef<HTMLElement>(null);
  /** A create request is on its way: its answer holds the only copy of the full key. */
  const creating = useRef(false);
  const shown = useRef(created);
  shown.current = created;
  const translate = useRef(t);
  translate.current = t;
  // The full key is shown once; leaving the page (or closing it) would lose it, as would
  // leaving while the request that returns it is still out. A blocked navigation says
  // why and brings this section into view.
  useEffect(() => {
    guard.current = async () => {
      if (!creating.current && !shown.current) return true;
      const section = root.current?.closest("details");
      if (section && !section.open) section.open = true;
      root.current?.scrollIntoView?.({ block: "start", behavior: "smooth" });
      return creating.current
        ? translate.current(
            "正在创建 API 密钥，完整密钥只会在创建完成时显示一次；请等它出现并保存后再离开。",
            "An API key is being created. Its full value is shown only once when it arrives; wait for it and save it before leaving.",
          )
        : translate.current(
            "新 API 密钥尚未关闭：请先保存完整密钥，再点“已保存，关闭显示”。",
            "The new API key is still shown. Save the full key, then choose “Saved; close display”.",
          );
    };
    const warn = (event: BeforeUnloadEvent) => {
      if (creating.current || shown.current) {
        event.preventDefault();
        event.returnValue = "";
      }
    };
    window.addEventListener("beforeunload", warn);
    return () => {
      guard.current = null;
      window.removeEventListener("beforeunload", warn);
    };
  }, [guard]);
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
        "供浏览器扩展使用：只能提交下载链接，不能浏览或修改书库。",
        "For the browser extension: can only submit download links; cannot browse or change the library.",
      ),
    ],
    management: [
      t("完全访问", "Full access"),
      t(
        "具有管理权限，包括修改书库和撤销其他密钥。",
        "Grants management access, including library changes and revoking other keys.",
      ),
    ],
  };
  const kindName = (value: string) => kinds[value as Kind]?.[0] ?? value;
  /** One action at a time; whatever happens, the list is reloaded afterwards. */
  async function act(task: () => Promise<void>) {
    if (busy) return;
    setNotice("");
    await action.run(task);
    await refresh();
  }
  const valid = label.trim().length > 0 && length(label.trim()) <= NAME_LIMIT;
  const create = () =>
    act(async () => {
      if (!valid || created) return;
      creating.current = true;
      try {
        setCreated(
          await post<Created>("/api/auth/keys", { label: label.trim(), kind }),
        );
      } catch (error) {
        // A lost response or server/proxy failure may follow a committed creation.
        if (uncertainWrite(error))
          setNotice(
            t(
              `无法确认密钥“${label.trim()}”是否已创建。如果下方列表里出现了它，其完整值已无法再显示，请撤销后重新创建。`,
              `Could not confirm whether the key “${label.trim()}” was created. If it appears in the list below, its full value cannot be shown again: revoke it and create a new one.`,
            ),
          );
        throw error;
      } finally {
        creating.current = false;
      }
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
    if (await copyText(created.key)) setCopied(true);
    else
      setError(
        new UserError(
          t(
            "复制失败，请选中完整密钥手动复制。",
            "Copy failed. Select the full key and copy it manually.",
          ),
        ),
      );
  }
  const problem = error ?? loadError;
  const failure = !!problem && (
    <p role="alert" className="notice error">
      {errorMessage(problem, t)}
    </p>
  );
  const listed = (keys ?? []).filter(
    (item) => showRevoked || item.revoked_at === null,
  );
  return (
    <section className="credentials-page" ref={root}>
      {failure}
      {notice && (
        <p role="status" className="notice">
          {notice}
        </p>
      )}
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
            maxLength={NAME_LIMIT}
            value={label}
            disabled={busy || !!created}
            onChange={(event) => setLabel(event.target.value)}
          />
        </label>
        <label>
          <span className="heading-with-info">
            {t("权限", "Permission")}
            {kind !== "management" && (
              <InfoTip label={t("关于密钥权限", "About key permissions")}>
                {kinds[kind][1]}
              </InfoTip>
            )}
          </span>
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
        {kind === "management" && <p className="notice">{kinds[kind][1]}</p>}
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
            {t(
              `权限：${kindName(created.kind)}。请保存到安全位置；关闭后无法再次读取完整值。`,
              `Permission: ${kindName(created.kind)}. Save it securely; the full value cannot be retrieved after closing.`,
            )}
          </p>
          <label>
            {t("完整 API 密钥", "Full API key")}
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
      <h4>{t("已有密钥", "Existing keys")}</h4>
      <label className="check-label">
        <input
          type="checkbox"
          checked={showRevoked}
          onChange={(event) => setShowRevoked(event.target.checked)}
        />
        {t("显示已撤销", "Show revoked")}
      </label>
      {!keys && !loadError && (
        <SkeletonRows label={t("正在读取…", "Loading…")} />
      )}
      {keys && !listed.length && (
        <p>{t("没有符合条件的密钥。", "No matching keys.")}</p>
      )}
      {listed.map((item) => (
        <article className="credential-item" key={item.id}>
          <div>
            <strong>{item.label}</strong>
            <p>
              {kindName(item.kind)} ·{" "}
              {item.revoked_at === null
                ? t("有效", "Active")
                : t("已撤销", "Revoked")}
            </p>
            <p className="muted small">
              {t("创建于", "Created")}{" "}
              {dateTime(item.created_at, t("zh-CN", "en-US"))}
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
          <div className="actions modal-actions">
            <button disabled={busy} onClick={() => setConfirm(null)}>
              {t("取消", "Cancel")}
            </button>
            <button
              className="danger"
              disabled={busy}
              onClick={() => void revoke(confirm)}
            >
              {t("确认撤销", "Confirm revocation")}
            </button>
          </div>
        </Modal>
      )}
    </section>
  );
}
