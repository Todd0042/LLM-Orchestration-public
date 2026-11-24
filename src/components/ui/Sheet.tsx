import { useEffect, useRef, type ReactNode } from "react";
import { X } from "lucide-react";

interface SheetProps {
  open: boolean;
  title: string;
  onClose: () => void;
  /** left/right: side drawer; full: covers the screen (phones); center: dialog */
  side?: "left" | "right" | "full" | "center";
  widthClass?: string;
  actions?: ReactNode;
  children: ReactNode;
}

/** Accessible overlay: Esc closes, focus moves in and returns, background is inert to clicks. */
export function Sheet({ open, title, onClose, side = "right", widthClass, actions, children }: SheetProps) {
  const panel = useRef<HTMLDivElement | null>(null);

  useEffect(() => {
    if (!open) return;
    const previous = document.activeElement as HTMLElement | null;
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") {
        e.stopPropagation();
        onClose();
      }
      if (e.key === "Tab" && panel.current) {
        const items = panel.current.querySelectorAll<HTMLElement>("button, [href], input, select, textarea, [tabindex]:not([tabindex='-1'])");
        if (!items.length) return;
        const first = items[0];
        const last = items[items.length - 1];
        if (e.shiftKey && document.activeElement === first) {
          e.preventDefault();
          last.focus();
        } else if (!e.shiftKey && document.activeElement === last) {
          e.preventDefault();
          first.focus();
        }
      }
    };
    window.addEventListener("keydown", onKey, true);
    window.setTimeout(() => panel.current?.querySelector<HTMLElement>("[data-autofocus], button, input, textarea")?.focus(), 30);
    return () => {
      window.removeEventListener("keydown", onKey, true);
      previous?.focus?.();
    };
  }, [open, onClose]);

  if (!open) return null;

  const pos =
    side === "left"
      ? "left-0 top-0 h-full"
      : side === "right"
        ? "right-0 top-0 h-full"
        : side === "full"
          ? "inset-0"
          : "left-1/2 top-1/2 -translate-x-1/2 -translate-y-1/2 max-h-[90dvh] rounded-xl";
  const width = widthClass ?? (side === "full" ? "w-full" : side === "center" ? "w-[min(35rem,94vw)]" : "w-[min(69rem,94vw)]");

  return (
    <div className="fixed inset-0 z-40" role="presentation">
      <div className="absolute inset-0 bg-black/60" onClick={onClose} aria-hidden="true" />
      <div
        ref={panel}
        role="dialog"
        aria-modal="true"
        aria-label={title}
        className={`absolute ${pos} ${width} flex flex-col border shadow-2xl overflow-hidden`}
        style={{ backgroundColor: "var(--bg-panel)", borderColor: "var(--border-strong)", paddingTop: "env(safe-area-inset-top)" }}
      >
        <div className="flex items-center justify-between gap-2 px-4 py-2.5 border-b shrink-0" style={{ borderColor: "var(--border-subtle)" }}>
          <h2 className="text-sm font-semibold truncate">{title}</h2>
          <div className="flex items-center gap-2">
            {actions}
            <button type="button" onClick={onClose} aria-label={`Close ${title}`} className="tap inline-flex items-center justify-center rounded-md border cursor-pointer"
                    style={{ borderColor: "var(--border-subtle)", backgroundColor: "var(--bg-elevated)" }}>
              <X className="w-4 h-4" />
            </button>
          </div>
        </div>
        <div className="flex-1 min-h-0 overflow-y-auto" style={{ paddingBottom: "env(safe-area-inset-bottom)" }}>
          {children}
        </div>
      </div>
    </div>
  );
}
