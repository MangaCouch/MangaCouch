import { RouteLink } from "./navigation";
import { useEffect, useRef, useState } from "react";
import { LoaderCircle } from "lucide-react";
import { request, aborted } from "./api";
import { Pagination } from "./Controls";
import { Pill } from "./Pills";
import { ErrorNotice } from "./feedback";
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
  after: string | null;
  through_id: string;
};
export function Maintenance({
  t,
  navigate,
  reveal = false,
}: {
  reveal?: boolean;
  t: Translate;
  navigate: (route: string) => void;
}) {
  const [items, setItems] = useState<Issue[]>([]),
    [busy, setBusy] = useState(false),
    [checked, setChecked] = useState<number | null>(null),
    [error, setError] = useState<unknown>(null),
    [page, setPage] = useState(1);
  const root = useRef<HTMLElement>(null);
  useEffect(() => {
    if (reveal) root.current?.scrollIntoView?.({ block: "start" });
  }, [reveal]);
  const controller = useRef<AbortController | null>(null);
  const checkpoint = useRef<{
    after: string;
    through_id: string | null;
  } | null>(null);
  useEffect(() => () => controller.current?.abort(), []);
  async function check() {
    if (busy) return;
    controller.current = new AbortController();
    const signal = controller.current.signal;
    setBusy(true);
    if (!checkpoint.current) {
      setItems([]);
      setChecked(0);
      setPage(1);
    }
    setError(null);
    let after = checkpoint.current?.after ?? "",
      through_id: string | null = checkpoint.current?.through_id ?? null;
    try {
      do {
        const result: CheckResult = await request("/api/removals/check", {
          method: "POST",
          body: JSON.stringify({ after, through_id }),
          signal,
        });
        if (signal.aborted) return;
        setItems((old) => [...old, ...result.items]);
        setChecked((n) => (n ?? 0) + result.checked);
        after = result.after ?? "";
        through_id = result.through_id;
        checkpoint.current = result.after ? { after, through_id } : null;
      } while (after);
    } catch (error) {
      if (!aborted(error)) setError(error);
    } finally {
      if (!signal.aborted) setBusy(false);
    }
  }
  return (
    <section ref={root}>
      <div className="section-head">
        <h3>{t("检查缺失文件", "Check missing files")}</h3>
        <button disabled={busy} onClick={() => void check()}>
          {busy && <LoaderCircle size={16} className="spinner" />}
          {checkpoint.current
            ? t("继续检查", "Continue check")
            : t("检查", "Check")}
        </button>
      </div>
      {checked !== null && (
        <p role="status">
          {t(
            `已检查 ${checked} 本，发现 ${items.length} 项异常`,
            `Checked ${checked} books; ${items.length} issues`,
          )}
        </p>
      )}
      <ErrorNotice error={error} t={t} />
      {items.slice((page - 1) * 20, page * 20).map((item) => (
        <div className="settings-row" key={item.book_id}>
          <RouteLink
            className="text-button"
            route={`book/${item.book_id}`}
            navigate={navigate}
          >
            {item.title}
          </RouteLink>
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
