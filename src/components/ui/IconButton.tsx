import type { ButtonHTMLAttributes, ReactNode } from "react";

interface IconButtonProps extends ButtonHTMLAttributes<HTMLButtonElement> {
  label: string;
  children: ReactNode;
  active?: boolean;
  showLabel?: boolean;
}

/** Icon button that always has an accessible name (and a visible one when showLabel). */
export function IconButton({ label, children, active, showLabel, className = "", style, ...rest }: IconButtonProps) {
  return (
    <button
      type="button"
      aria-label={label}
      title={label}
      className={`tap inline-flex items-center justify-center gap-1.5 rounded-md border text-xs font-medium cursor-pointer disabled:opacity-40 ${showLabel ? "px-2.5" : ""} ${className}`}
      style={{
        backgroundColor: active ? "var(--accent-primary-soft)" : "var(--bg-elevated)",
        borderColor: active ? "var(--accent-primary)" : "var(--border-subtle)",
        color: active ? "var(--accent-primary)" : "var(--text-primary)",
        ...style,
      }}
      {...rest}
    >
      {children}
      {showLabel && <span>{label}</span>}
    </button>
  );
}
