import { useEffect, useRef, useState } from "react";
import { LoaderCircle } from "lucide-react";
import { request, errorMessage, aborted } from "./api";
import { Pagination } from "./Controls";
import { Pill } from "./Pills";
import type { Translate } from "./types";
type Issue = {
  book_id: string;
  title: string;
  relative_path: string;
  state: string;
};
type CheckResult = {
  items: Issue[];
  checked: number;
  after_id: string | null;
  through_id: string;
};
export function Maintenance({
  t,
  navigate,
}: {
  t: Translate;
  navigate: (route: string) => void;
}) {
  const [items, setItems] = useState<Issue[]>([]),
    [busy, setBusy] = useState(false),
    [checked, setChecked] = useState<number | null>(null),
    [error, setError] = useState<unknown>(null),
    [page, setPage] = useState(1);
  const controller = useRef<AbortController | null>(null);
  useEffect(() => () => controller.current?.abort(), []);
  async function check() {
    if (busy) return;
    controller.current = new AbortController();
    const signal = controller.current.signal;
    setBusy(true);
    setItems([]);
    setChecked(0);
    setPage(1);
    setError(null);
    let after_id = "",
      through_id: string | null = null;
    try {
      do {
        const result: CheckResult = await request("/api/removals/check", {
          method: "POST",
          body: JSON.stringify({ after_id, through_id }),
          signal,
        });
        if (signal.aborted) return;
        setItems((old) => [...old, ...result.items]);
        setChecked((n) => (n ?? 0) + result.checked);
        after_id = result.after_id ?? "";
        through_id = result.through_id;
      } while (after_id);
    } catch (error) {
      if (!aborted(error)) setError(error);
    } finally {
      if (!signal.aborted) setBusy(false);
    }
  }
  return (
    <section>
      <button disabled={busy} onClick={() => void check()}>
        {busy && <LoaderCircle size={16} className="spinner" />}
        {t("检查缺失文件", "Check missing files")}
      </button>
      {checked !== null && (
        <p role="status">
          {t(
            `已检查 ${checked} 本，发现 ${items.length} 项异常`,
            `Checked ${checked} books; ${items.length} issues`,
          )}
        </p>
      )}
      {!!error && <p className="error-text">{errorMessage(error, t)}</p>}
      {items.slice((page - 1) * 20, page * 20).map((item) => (
        <div className="settings-row" key={item.book_id}>
          <button
            className="text-button"
            onClick={() => navigate(`book/${item.book_id}`)}
          >
            {item.title}
          </button>
          <Pill>
            {item.state === "missing"
              ? t("文件缺失", "Missing")
              : item.state === "changed"
                ? t("内容变化", "Changed")
                : t("无法读取", "Unreadable")}
          </Pill>
        </div>
      ))}
      {items.length > 20 && (
        <Pagination
          page={page}
          total={items.length}
          size={20}
          change={setPage}
          t={t}
        />
      )}
    </section>
  );
}
