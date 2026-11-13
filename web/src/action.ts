import { useCallback, useRef, useState } from "react";
import { aborted } from "./api";

/**
 * One user action at a time, with its busy flag and failure. `run` ignores a call while
 * another is in flight, clears the previous failure and resolves to whether the task succeeded.
 */
export function useAction() {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<unknown>(null);
  const working = useRef(false);
  const run = useCallback(async (task: () => Promise<unknown>) => {
    if (working.current) return false;
    working.current = true;
    setBusy(true);
    setError(null);
    try {
      await task();
      return true;
    } catch (failure) {
      if (!aborted(failure)) setError(failure);
      return false;
    } finally {
      working.current = false;
      setBusy(false);
    }
  }, []);
  return { busy, error, setError, run };
}
