// State tests use semantic adapters; browser tests cover Radix layout and focus.
// Browser acceptance exercises the actual Radix components.
import React, { createContext, useContext, useState } from "react";
const node = React.createElement;
const walk = (children, type) =>
  React.Children.toArray(children).flatMap((child) =>
    React.isValidElement(child)
      ? [
          ...(child.type === type ? [child] : []),
          ...walk(child.props.children, type),
        ]
      : [],
  );
const passthrough = ({ children }) => children;
const item = () => null,
  trigger = () => null;
export const Select = {
  Root: ({ children, value, defaultValue, onValueChange, disabled }) => {
    const button = walk(children, trigger)[0];
    return node(
      "select",
      {
        ...button?.props,
        children: undefined,
        value: value === "__empty__" ? "" : value,
        defaultValue,
        disabled,
        onChange: (event) => onValueChange(event.target.value || "__empty__"),
      },
      walk(children, item).map((option, index) =>
        node(
          "option",
          {
            key: index,
            value: option.props.value === "__empty__" ? "" : option.props.value,
            disabled: option.props.disabled,
          },
          walk(option.props.children, Select.ItemText)[0]?.props.children,
        ),
      ),
    );
  },
  Trigger: trigger,
  Portal: passthrough,
  Content: passthrough,
  Viewport: passthrough,
  Item: item,
  ItemText: passthrough,
  ItemIndicator: passthrough,
  Value: () => null,
  Icon: passthrough,
};
const DialogContext = createContext(() => {});
export const Dialog = {
  Root: ({ children, onOpenChange }) =>
    node(DialogContext.Provider, { value: onOpenChange }, children),
  Portal: passthrough,
  Overlay: () => null,
  Content: ({
    children,
    onEscapeKeyDown,
    onCloseAutoFocus: _focus,
    onInteractOutside: _outside,
    ...props
  }) => {
    const change = useContext(DialogContext);
    return node(
      "dialog",
      {
        ...props,
        open: true,
        role: "dialog",
        onCancel: (event) => {
          onEscapeKeyDown?.(event);
          if (!event.defaultPrevented) change(false);
        },
      },
      children,
    );
  },
  Title: ({ children }) => children,
  Close: ({ children }) => {
    const change = useContext(DialogContext);
    return React.cloneElement(children, { onClick: () => change(false) });
  },
};
function menu() {
  const Context = createContext(null);
  return {
    Root: ({ children }) => {
      const [open, setOpen] = useState(false);
      return node(Context.Provider, { value: { open, setOpen } }, children);
    },
    Trigger: React.forwardRef(({ children, asChild, ...props }, ref) => {
      const context = useContext(Context);
      return asChild
        ? React.cloneElement(children, { ref })
        : node(
            "button",
            { ...props, ref, onClick: () => context.setOpen(!context.open) },
            children,
          );
    }),
    Portal: passthrough,
    Content: ({
      children,
      sideOffset: _offset,
      collisionPadding: _padding,
      onCloseAutoFocus: _focus,
      ...props
    }) => (useContext(Context).open ? node("div", props, children) : null),
    Item: ({ children, onSelect, ...props }) => {
      const context = useContext(Context);
      return node(
        "button",
        {
          ...props,
          onClick: () => {
            onSelect();
            context.setOpen(false);
          },
        },
        children,
      );
    },
  };
}
export const DropdownMenu = menu(),
  ContextMenu = menu(),
  Popover = menu();
