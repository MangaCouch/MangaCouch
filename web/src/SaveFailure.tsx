import { useState } from "react";
import { LoaderCircle } from "lucide-react";
import type { PersonalQueue } from "./personal";
import type { Translate } from "./types";

/** Personal changes failed to save: retry them, or discard them and reload. */
export function SaveFailure({
  queue,
  lead,
  className,
  t,
}: {
  queue: PersonalQueue;
  /** What failed, in the words of the page showing it. */
  lead: string;
  className: string;
  t: Translate;
}) {
  const [pending, setPending] = useState<"retry" | "reload" | null>(null);
  async function act(kind: "retry" | "reload") {
    setPending(kind);
    try {
      await (kind === "retry" ? queue.retry() : queue.reload());
    } finally {
      setPending(null);
    }
  }
  const spinner = (kind: "retry" | "reload") =>
    pending === kind && (
      <LoaderCircle size={15} className="spinner" aria-hidden="true" />
    );
  return (
    <div className={className} role="alert" aria-busy={pending !== null}>
      {lead}{" "}
      {queue.conflicted() &&
        t(
          "另一台设备修改了评分或已读状态；重试会用你的修改覆盖它们。",
          "Another device changed the rating or finished state; retrying overwrites them with yours.",
        )}{" "}
      <button
        type="button"
        disabled={pending !== null}
        onClick={() => void act("retry")}
      >
        {spinner("retry")}
        {t("重试保存", "Retry saving")}
      </button>{" "}
      <button
        type="button"
        disabled={pending !== null}
        onClick={() => void act("reload")}
      >
        {spinner("reload")}
        {t("丢弃未保存修改并重新载入", "Discard unsaved changes and reload")}
      </button>
    </div>
  );
}
