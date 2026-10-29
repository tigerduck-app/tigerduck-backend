import { toast } from "sonner";

// The portal is also opened over plain HTTP on the LAN, where browsers leave
// navigator.clipboard undefined; a selected hidden textarea still copies there.
function copyWithSelection(text: string): boolean {
  const focused = document.activeElement as HTMLElement | null;
  const area = document.createElement("textarea");
  area.value = text;
  area.setAttribute("readonly", "");
  area.style.position = "fixed";
  area.style.opacity = "0";
  document.body.appendChild(area);
  area.select();
  try {
    return document.execCommand("copy");
  } catch {
    return false;
  } finally {
    area.remove();
    focused?.focus();
  }
}

async function write(text: string): Promise<boolean> {
  if (navigator.clipboard?.writeText) {
    try {
      await navigator.clipboard.writeText(text);
      return true;
    } catch {
      // Denied permission: try the selection copy below.
    }
  }
  return copyWithSelection(text);
}

/** Copies `text` and toasts `done`, or "Copy failed" when no way works. */
export function copyText(text: string, done: string): void {
  void write(text).then((ok) => (ok ? toast.success(done) : toast.error("Copy failed")));
}
