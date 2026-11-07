import { Children, isValidElement, useId, useState } from "react";
import type { ChangeEvent, ReactNode, SelectHTMLAttributes } from "react";
import {
  Select as RadixSelect,
  Popover,
  DropdownMenu,
  ContextMenu,
} from "radix-ui";
import {
  Check,
  ChevronDown,
  ChevronsLeft,
  ChevronsRight,
  ChevronLeft,
  ChevronRight,
  MoreVertical,
} from "lucide-react";
import type { Translate } from "./types";

export function Select({
  children,
  value,
  defaultValue,
  onChange,
  className = "",
  ...props
}: SelectHTMLAttributes<HTMLSelectElement>) {
  const options = Children.toArray(children).filter(
    isValidElement,
  ) as React.ReactElement<{
    value: string | number;
    children: ReactNode;
    disabled?: boolean;
  }>[];
  const key = (v: unknown) => String(v ?? "") || "__empty__";
  return (
    <RadixSelect.Root
      value={value === undefined ? undefined : key(value)}
      defaultValue={defaultValue === undefined ? undefined : key(defaultValue)}
      disabled={props.disabled}
      onValueChange={(v) =>
        onChange?.({
          target: { value: v === "__empty__" ? "" : v },
          currentTarget: { value: v === "__empty__" ? "" : v },
        } as ChangeEvent<HTMLSelectElement>)
      }
    >
      <RadixSelect.Trigger
        className={`select-control ${className}`}
        id={props.id}
        aria-label={props["aria-label"]}
        aria-labelledby={props["aria-labelledby"]}
        title={props.title}
        name={props.name}
      >
        <RadixSelect.Value />
        <RadixSelect.Icon>
          <ChevronDown size={16} />
        </RadixSelect.Icon>
      </RadixSelect.Trigger>
      <RadixSelect.Portal>
        <RadixSelect.Content
          className="select-popover glass"
          position="popper"
          sideOffset={5}
          collisionPadding={12}
        >
          <RadixSelect.Viewport>
            {options.map((option, index) => (
              <RadixSelect.Item
                className="select-option"
                key={index}
                value={key(option.props.value ?? option.props.children)}
                disabled={option.props.disabled}
              >
                <RadixSelect.ItemText>
                  {option.props.children}
                </RadixSelect.ItemText>
                <RadixSelect.ItemIndicator>
                  <Check size={16} />
                </RadixSelect.ItemIndicator>
              </RadixSelect.Item>
            ))}
          </RadixSelect.Viewport>
        </RadixSelect.Content>
      </RadixSelect.Portal>
    </RadixSelect.Root>
  );
}

export function Segmented({
  value,
  options,
  change,
  label,
}: {
  value: string;
  options: { value: string; label: string; disabled?: boolean }[];
  change: (v: string) => void;
  label: string;
}) {
  return (
    <div className="segmented" role="group" aria-label={label}>
      {options.map((option) => (
        <button
          type="button"
          key={option.value}
          disabled={option.disabled}
          aria-pressed={value === option.value}
          onClick={() => change(option.value)}
        >
          {option.label}
        </button>
      ))}
    </div>
  );
}

type MenuAction = {
  label: string;
  action: () => void;
  danger?: boolean;
  disabled?: boolean;
};
export function ActionMenu({
  actions,
  label,
}: {
  actions: MenuAction[];
  label: string;
}) {
  return (
    <DropdownMenu.Root>
      <DropdownMenu.Trigger className="icon-button" aria-label={label}>
        <MoreVertical size={20} />
      </DropdownMenu.Trigger>
      <DropdownMenu.Portal>
        <DropdownMenu.Content
          className="action-menu glass"
          onCloseAutoFocus={(event) => {
            if (document.querySelector('[role="dialog"]'))
              event.preventDefault();
          }}
          sideOffset={5}
          collisionPadding={12}
        >
          {actions.map((item, i) => (
            <DropdownMenu.Item
              className={item.danger ? "danger" : ""}
              key={i}
              disabled={item.disabled}
              onSelect={item.action}
            >
              {item.label}
            </DropdownMenu.Item>
          ))}
        </DropdownMenu.Content>
      </DropdownMenu.Portal>
    </DropdownMenu.Root>
  );
}
export function ContextActions({
  actions,
  children,
}: {
  actions: MenuAction[];
  children: ReactNode;
}) {
  const triggerId = useId();
  return (
    <ContextMenu.Root>
      <ContextMenu.Trigger id={triggerId} asChild>
        {children}
      </ContextMenu.Trigger>
      <ContextMenu.Portal>
        <ContextMenu.Content
          className="action-menu glass"
          data-return-focus={triggerId}
          collisionPadding={12}
          onCloseAutoFocus={(event) => {
            if (document.querySelector('[role="dialog"]'))
              event.preventDefault();
          }}
        >
          {actions.map((item, i) => (
            <ContextMenu.Item
              key={i}
              className={item.danger ? "danger" : ""}
              disabled={item.disabled}
              onSelect={item.action}
            >
              {item.label}
            </ContextMenu.Item>
          ))}
        </ContextMenu.Content>
      </ContextMenu.Portal>
    </ContextMenu.Root>
  );
}
export function Pagination({
  page,
  total,
  size,
  change,
  t,
  busy = false,
}: {
  page: number;
  total: number;
  size: number;
  change: (n: number) => void;
  t: Translate;
  busy?: boolean;
}) {
  const pages = Math.max(1, Math.ceil(total / size));
  const [open, setOpen] = useState(false);
  const [target, setTarget] = useState(String(page));
  const id = useId();
  return (
    <nav className="pagination" aria-label={t("分页", "Pagination")}>
      <button
        type="button"
        disabled={busy || page <= 1}
        onClick={() => change(1)}
        aria-label={t("首页", "First page")}
      >
        <ChevronsLeft size={16} />
      </button>
      <button
        type="button"
        disabled={busy || page <= 1}
        onClick={() => change(page - 1)}
        aria-label={t("上一页", "Previous page")}
      >
        <ChevronLeft size={16} />
      </button>
      <Popover.Root
        open={open}
        onOpenChange={(value) => {
          setTarget(String(page));
          setOpen(value);
        }}
      >
        <Popover.Trigger
          disabled={busy}
          aria-label={t("跳转页码", "Jump to page")}
        >
          {page} / {pages}
        </Popover.Trigger>
        <Popover.Portal>
          <Popover.Content
            className="jump-popover glass"
            sideOffset={8}
            collisionPadding={12}
          >
            <form
              onSubmit={(event) => {
                event.preventDefault();
                const next = Number(target);
                if (Number.isInteger(next) && next >= 1 && next <= pages) {
                  change(next);
                  setOpen(false);
                }
              }}
            >
              <label htmlFor={id}>{t("跳到页", "Page")}</label>
              <input
                id={id}
                type="number"
                min={1}
                max={pages}
                value={target}
                onChange={(e) => setTarget(e.target.value)}
                autoFocus
              />
              <button type="submit" className="primary">
                {t("跳转", "Go")}
              </button>
            </form>
          </Popover.Content>
        </Popover.Portal>
      </Popover.Root>
      <button
        type="button"
        disabled={busy || page >= pages}
        onClick={() => change(page + 1)}
        aria-label={t("下一页", "Next page")}
      >
        <ChevronRight size={16} />
      </button>
      <button
        type="button"
        disabled={busy || page >= pages}
        onClick={() => change(pages)}
        aria-label={t("末页", "Last page")}
      >
        <ChevronsRight size={16} />
      </button>
    </nav>
  );
}
