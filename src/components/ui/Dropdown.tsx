import { useEffect, useRef, useState } from "react";
import { Check, ChevronDown } from "lucide-react";

export interface DropdownOption {
  value: string;
  label: string;
  badge?: string;
  hint?: string;
  group?: string;
}

interface DropdownProps {
  value: string;
  options: DropdownOption[];
  onChange: (value: string) => void;
  placeholder?: string;
  menuWidthClass?: string;
  placement?: "down" | "up";
  align?: "left" | "right";
  title?: string;
}

export function Dropdown({
  value,
  options,
  onChange,
  placeholder = "Select…",
  menuWidthClass = "min-w-[14.375rem]",
  placement = "down",
  align = "left",
  title,
}: DropdownProps) {
  const [open, setOpen] = useState(false);
  const rootRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (!open) return;
    const onDown = (e: MouseEvent) => {
      if (rootRef.current && !rootRef.current.contains(e.target as Node)) setOpen(false);
    };
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") setOpen(false);
    };
    window.addEventListener("mousedown", onDown);
    window.addEventListener("keydown", onKey);
    return () => {
      window.removeEventListener("mousedown", onDown);
      window.removeEventListener("keydown", onKey);
    };
  }, [open]);

  const current = options.find((o) => o.value === value);

  return (
    <div ref={rootRef} className="relative inline-block max-w-full min-w-0">
      <button
        type="button"
        title={title}
        onClick={() => setOpen((v) => !v)}
        className="w-full min-w-0 flex items-center justify-between gap-1.5 px-3 py-2 lg:px-2.5 lg:py-1 rounded-md border text-sm lg:text-xs font-medium cursor-pointer"
        style={{
          backgroundColor: "var(--bg-elevated)",
          borderColor: open ? "var(--accent-primary)" : "var(--border-subtle)",
          color: "var(--text-primary)",
        }}
      >
        <span className="truncate flex-1 min-w-0 text-left">{current?.label ?? placeholder}</span>
        {current?.badge && (
          <span
            className="text-[0.625rem] px-1.5 py-0.2 rounded font-semibold shrink-0"
            style={{ backgroundColor: "var(--accent-primary-soft)", color: "var(--accent-primary)" }}
          >
            {current.badge}
          </span>
        )}
        <ChevronDown className="w-3.5 h-3.5 shrink-0" style={{ color: "var(--text-secondary)" }} />
      </button>

      {open && (
        <div
          className={`absolute z-50 rounded-lg border shadow-2xl py-1 max-h-72 overflow-y-auto max-w-[calc(100vw-24px)] ${
            placement === "up" ? "bottom-full mb-1" : "top-full mt-1"
          } ${align === "right" ? "right-0" : "left-0"} ${menuWidthClass}`}
          style={{
            backgroundColor: "var(--bg-elevated)",
            borderColor: "var(--border-strong)",
            color: "var(--text-primary)",
          }}
        >
          {options.map((opt, idx) => {
            const selected = opt.value === value;
            const showGroup = Boolean(opt.group && (idx === 0 || options[idx - 1]?.group !== opt.group));
            return (
              <div key={opt.value}>
                {showGroup && (
                  <div
                    className="px-3 py-1 text-[0.625rem] font-semibold uppercase tracking-wider border-t first:border-t-0"
                    style={{
                      color: "var(--text-muted)",
                      borderColor: "var(--border-subtle)",
                      backgroundColor: "var(--bg-muted)",
                    }}
                  >
                    {opt.group}
                  </div>
                )}
                <button
                  type="button"
                  onClick={() => {
                    onChange(opt.value);
                    setOpen(false);
                  }}
                  className={`w-full flex items-center justify-between gap-2 px-3 py-2.5 lg:py-1.5 text-sm lg:text-xs text-left cursor-pointer ${
                    selected ? "bg-sky-500/15 font-semibold" : "hover:bg-white/[0.06]"
                  }`}
                  style={{ color: "var(--text-primary)" }}
                >
                  <div className="min-w-0">
                    <div className="truncate">{opt.label}</div>
                    {opt.hint && (
                      <div className="text-[0.625rem] truncate" style={{ color: "var(--text-muted)" }}>
                        {opt.hint}
                      </div>
                    )}
                  </div>
                  <span className="inline-flex items-center gap-1.5 shrink-0">
                    {opt.badge && (
                      <span
                        className="text-[0.625rem] px-1.5 py-0.5 rounded font-semibold"
                        style={{
                          backgroundColor: "var(--accent-primary-soft)",
                          color: "var(--accent-primary)",
                        }}
                      >
                        {opt.badge}
                      </span>
                    )}
                    {selected && <Check className="w-3.5 h-3.5" style={{ color: "var(--accent-primary)" }} />}
                  </span>
                </button>
              </div>
            );
          })}
        </div>
      )}
    </div>
  );
}
