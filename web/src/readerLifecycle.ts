import { useEffect, useRef, useState } from "react";
import type { Autoplay } from "./reading";
import type { PersonalQueue, SaveStatus } from "./personal";
import type { ViewState } from "./PageImage";

/** Reading progress is saved this long after the page stops changing. */
export const PROGRESS_SAVE_DELAY_MS = 1500;
/** The "Saving" pill only appears for saves that take longer than this. */
export const SAVING_PILL_DELAY_MS = 1000;
export function useFullscreen() {
  const ownsFullscreen = useRef(false);
  const [fullscreenNote, setFullscreenNote] = useState(false);
  const [fullscreenActive, setFullscreenActive] = useState(
    () => !!document.fullscreenElement,
  );
  useEffect(() => {
    const change = () => setFullscreenActive(!!document.fullscreenElement);
    document.addEventListener("fullscreenchange", change);
    return () => {
      document.removeEventListener("fullscreenchange", change);
      if (ownsFullscreen.current && document.fullscreenElement)
        void document.exitFullscreen().catch(() => {});
    };
  }, []);
  async function fullscreen() {
    // The whole document goes fullscreen so portalled menus and dialogs stay visible.
    const element = document.documentElement;
    if (!document.fullscreenEnabled || !element?.requestFullscreen) {
      setFullscreenNote(true);
      return;
    }
    try {
      if (document.fullscreenElement) await document.exitFullscreen();
      else {
        await element.requestFullscreen({ navigationUI: "hide" });
        ownsFullscreen.current = true;
      }
    } catch {
      setFullscreenNote(true);
    }
  }
  return { fullscreenNote, setFullscreenNote, fullscreenActive, fullscreen };
}

export function useReaderLifecycle(playback: Autoplay, queue: PersonalQueue) {
  const [visible, setVisible] = useState(!document.hidden);
  useEffect(() => {
    const visibility = () => {
      setVisible(!document.hidden);
      if (document.hidden) {
        playback.pause();
        // A hidden tab may never come back; the save must outlive the page.
        void queue.flush(true);
      }
    };
    const leave = () => {
      playback.pause();
      void queue.flush();
    };
    const unload = () => {
      playback.pause();
      void queue.flush(true);
    };
    document.addEventListener("visibilitychange", visibility);
    window.addEventListener("pagehide", unload);
    window.addEventListener("hashchange", leave);
    return () => {
      document.removeEventListener("visibilitychange", visibility);
      window.removeEventListener("pagehide", unload);
      window.removeEventListener("hashchange", leave);
      playback.dispose();
      void queue.flush();
    };
  }, [playback, queue]);
  return visible;
}

export function useReadingProgress(
  queue: PersonalQueue,
  current: ViewState | null,
  visible: boolean,
  status: SaveStatus,
  last: number,
) {
  const readyPage = current?.page,
    activeReady = current?.activeReady,
    completed = current?.completed;
  useEffect(() => {
    if (
      !activeReady ||
      !visible ||
      readyPage === undefined ||
      queue.current().personal.completed
    )
      return;
    // Progress is debounced; finishing the book is saved at once. A finished book is
    // saved at its last page whatever the layout (a final spread shows from the page
    // before it), so the next visit can tell it was read to the end.
    queue.update(
      (state) => ({
        ...state,
        resume_page: completed ? last : readyPage,
        completed: state.completed || !!completed,
      }),
      completed ? 0 : PROGRESS_SAVE_DELAY_MS,
      true,
    );
  }, [readyPage, activeReady, completed, visible, queue, last]);
  const [slowSave, setSlowSave] = useState(false);
  useEffect(() => {
    setSlowSave(false);
    if (status !== "saving") return;
    const timer = setTimeout(() => setSlowSave(true), SAVING_PILL_DELAY_MS);
    return () => clearTimeout(timer);
  }, [status]);
  return slowSave;
}
