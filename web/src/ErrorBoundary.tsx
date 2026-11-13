import { Component, type ErrorInfo, type ReactNode } from "react";
import { RotateCw } from "lucide-react";
import type { Translate } from "./types";

/**
 * Whether `error` is a lazily loaded part of the app that could not be fetched: after a
 * server upgrade the old build's chunks are gone, so an open page cannot load them.
 */
export function chunkFailure(error: unknown): boolean {
  const { name, message } = (error ?? {}) as {
    name?: unknown;
    message?: unknown;
  };
  return (
    name === "ChunkLoadError" ||
    (typeof message === "string" &&
      /Failed to fetch dynamically imported module|error loading dynamically imported module|Importing a module script failed|Unable to preload CSS/i.test(
        message,
      ))
  );
}

type Props = {
  t: Translate;
  /** A change (e.g. the route) clears a caught error and renders the children again. */
  resetKey?: string;
  /** A way out besides reloading, e.g. leaving the reader. */
  back?: { label: string; action: () => void };
  children: ReactNode;
};
type State = { error: unknown; key: string | undefined };

/** Catches render errors and resource failures without leaving the page automatically. */
export class ErrorBoundary extends Component<Props, State> {
  state: State = { error: null, key: this.props.resetKey };
  static getDerivedStateFromError(error: unknown): Partial<State> {
    return { error: error ?? new Error("Unknown error") };
  }
  static getDerivedStateFromProps(props: Props, state: State) {
    return props.resetKey === state.key
      ? null
      : { error: null, key: props.resetKey };
  }
  componentDidCatch(error: unknown, info: ErrorInfo) {
    console.error("Interface error", error, info.componentStack);
  }
  render() {
    const { t, back, children } = this.props;
    if (!this.state.error) return children;
    return (
      <main className="content interface-error" role="alert">
        <h1>
          {chunkFailure(this.state.error)
            ? t("页面资源加载失败", "Page resources could not load")
            : t("界面遇到错误", "The page hit an error")}
        </h1>
        <p className="muted">
          {back
            ? t(
                "请检查网络连接后重试。重新载入可能丢失尚未保存的输入，也可以先返回上一页。",
                "Check your connection before retrying. Reloading may lose unsaved input; you can also go back first.",
              )
            : t(
                "请检查网络连接后重试。重新载入可能丢失尚未保存的输入。",
                "Check your connection before retrying. Reloading may lose unsaved input.",
              )}
        </p>
        <div className="actions">
          <button
            type="button"
            className="primary"
            onClick={() => location.reload()}
          >
            <RotateCw size={17} aria-hidden="true" />
            {t("重新载入", "Reload")}
          </button>
          {back && (
            <button type="button" onClick={back.action}>
              {back.label}
            </button>
          )}
        </div>
      </main>
    );
  }
}
