import { useEffect, useState } from "react";
import type { Autoplay } from "./reading";
import type { PersonalQueue, SaveStatus } from "./personal";
import type { ViewState } from "./PageImage";

/** Reading progress is saved this long after the page stops changing. */
export const PROGRESS_SAVE_DELAY_MS = 1500;
/** The "Saving" pill only appears for saves that take longer than this. */
export const SAVING_PILL_DELAY_MS = 1000;
export function useFullscreen() {
  const [fullscreenNote, setFullscreenNote] = useState(false);
  const [fullscreenActive, setFullscreenActive] = useState(
    () => !!document.fullscreenElement,
  );
  useEffect(() => {
    const change = () => setFullscreenActive(!!document.fullscreenElement);
    document.addEventListener("fullscreenchange", change);
    return () => document.removeEventListener("fullscreenchange", change);
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
      else await element.requestFullscreen({ navigationUI: "hide" });
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
        void queue.flush();
      }
    };
    const leave = () => {
      playback.pause();
      void queue.flush();
    };
    document.addEventListener("visibilitychange", visibility);
    window.addEventListener("pagehide", leave);
    window.addEventListener("hashchange", leave);
    return () => {
      document.removeEventListener("visibilitychange", visibility);
      window.removeEventListener("pagehide", leave);
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
) {
  const readyPage = current?.page,
    activeReady = current?.activeReady,
    completed = current?.completed;
  useEffect(() => {
    if (!activeReady || !visible || readyPage === undefined) return;
    // Progress is debounced; finishing the book is saved at once.
    queue.update(
      (state) => ({
        ...state,
        resume_page: readyPage,
        completed: state.completed || !!completed,
      }),
      completed ? 0 : PROGRESS_SAVE_DELAY_MS,
    );
  }, [readyPage, activeReady, completed, visible, queue]);
  const [slowSave, setSlowSave] = useState(false);
  useEffect(() => {
    setSlowSave(false);
    if (status !== "saving") return;
    const timer = setTimeout(() => setSlowSave(true), SAVING_PILL_DELAY_MS);
    return () => clearTimeout(timer);
  }, [status]);
  return slowSave;
}
