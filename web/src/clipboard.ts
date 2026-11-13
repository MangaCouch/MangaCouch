/**
 * Copies text and resolves to whether it worked. `navigator.clipboard` only exists on
 * secure origins, so a library opened over plain HTTP on the LAN copies through a
 * selected text area instead.
 */
export async function copyText(text: string) {
  try {
    if (navigator.clipboard) {
      await navigator.clipboard.writeText(text);
      return true;
    }
  } catch {
    /* Denied: try the text area below. */
  }
  const focused = document.activeElement as HTMLElement | null;
  const area = document.createElement("textarea");
  area.value = text;
  area.setAttribute("readonly", "");
  area.style.position = "fixed";
  area.style.opacity = "0";
  document.body.append(area);
  area.select();
  try {
    return document.execCommand("copy");
  } catch {
    return false;
  } finally {
    area.remove();
    focused?.focus?.();
  }
}
