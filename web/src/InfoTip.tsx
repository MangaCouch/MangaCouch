import type { ReactNode } from "react";
import { Info } from "lucide-react";
import { Popover } from "radix-ui";

export function InfoTip({
  label,
  children,
}: {
  label: string;
  children: ReactNode;
}) {
  return (
    <Popover.Root>
      <Popover.Trigger
        type="button"
        className="icon-button info-tip"
        aria-label={label}
      >
        <Info size={16} aria-hidden="true" />
      </Popover.Trigger>
      <Popover.Portal>
        <Popover.Content
          className="info-popover glass"
          sideOffset={8}
          collisionPadding={12}
          aria-label={label}
        >
          {children}
        </Popover.Content>
      </Popover.Portal>
    </Popover.Root>
  );
}
