import { useEffect } from "react";
import { Toaster, toast } from "sonner";
import type { Translate } from "./types";

export { toast };

export function useFeedback(error: string, success = "") {
  useEffect(() => {
    if (error) toast.error(error, { id: `error:${error}`, duration: 8000 });
  }, [error]);
  useEffect(() => {
    if (success) toast.success(success, { id: `success:${success}` });
  }, [success]);
}

export function Notifications({ t }: { t: Translate }) {
  return (
    <Toaster
      position="top-right"
      richColors
      closeButton
      visibleToasts={3}
      gap={10}
      duration={4500}
      offset={24}
      mobileOffset={12}
      containerAriaLabel={t("通知", "Notifications")}
      toastOptions={{
        className: "app-toast",
        closeButtonAriaLabel: t("关闭通知", "Dismiss notification"),
      }}
    />
  );
}
