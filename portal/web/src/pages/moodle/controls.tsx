// Small controls the Moodle pages repeat: segmented buttons, underline tabs,
// filter chips and the banner a failed load shows.

import type { ReactNode } from "react";
import { RefreshCw, TriangleAlert } from "lucide-react";
import { cn } from "@/lib/cn";

export type Option<V extends string> = { value: V; label: ReactNode; count?: number };

export function Segmented<V extends string>({
  label,
  options,
  value,
  onChange,
  radio = false,
  className,
  itemClassName,
}: {
  label: string;
  options: readonly Option<V>[];
  value: V;
  onChange: (v: V) => void;
  /** A choice in a form reads as radios; a view switch as pressed buttons. */
  radio?: boolean;
  className?: string;
  itemClassName?: string;
}) {
  return (
    <div
      role={radio ? "radiogroup" : "group"}
      aria-label={label}
      className={cn("flex gap-0.5 rounded-[9px] bg-soft p-[3px]", className)}
    >
      {options.map((o) => {
        const on = o.value === value;
        return (
          <button
            key={o.value}
            type="button"
            role={radio ? "radio" : undefined}
            aria-checked={radio ? on : undefined}
            aria-pressed={radio ? undefined : on}
            onClick={() => onChange(o.value)}
            className={cn(
              "flex min-h-[30px] items-center justify-center gap-1.5 whitespace-nowrap rounded-[7px] px-3 text-[13px]",
              on
                ? "bg-surface font-semibold text-ink shadow-[0_1px_2px_rgba(20,22,26,0.12)]"
                : "text-ink-2 hover:text-ink",
              itemClassName,
            )}
          >
            {o.label}
            {o.count !== undefined && <span className="text-[11px] font-normal text-ink-3">{o.count}</span>}
          </button>
        );
      })}
    </div>
  );
}

export function TabBar<V extends string>({
  label,
  tabs,
  value,
  onChange,
  className,
}: {
  label: string;
  tabs: readonly Option<V>[];
  value: V;
  onChange: (v: V) => void;
  className?: string;
}) {
  return (
    <div role="tablist" aria-label={label} className={cn("flex gap-1", className)}>
      {tabs.map((t) => {
        const on = t.value === value;
        return (
          <button
            key={t.value}
            type="button"
            role="tab"
            aria-selected={on}
            onClick={() => onChange(t.value)}
            className={cn(
              "flex min-h-10 shrink-0 items-center gap-1.5 whitespace-nowrap border-b-2 px-3",
              on ? "border-ink font-semibold text-ink" : "border-transparent font-medium text-ink-3 hover:text-ink",
            )}
          >
            {t.label}
            {t.count !== undefined && <span className="text-xs font-normal text-ink-3">{t.count}</span>}
          </button>
        );
      })}
    </div>
  );
}

export function Chips<V extends string>({
  label,
  options,
  isOn,
  onPick,
  className,
  mono = false,
}: {
  label: string;
  options: readonly Option<V>[];
  isOn: (v: V) => boolean;
  onPick: (v: V) => void;
  className?: string;
  mono?: boolean;
}) {
  return (
    <div role="group" aria-label={label} className={cn("flex flex-wrap gap-1.5", className)}>
      {options.map((o) => {
        const on = isOn(o.value);
        return (
          <button
            key={o.value}
            type="button"
            aria-pressed={on}
            onClick={() => onPick(o.value)}
            className={cn(
              "inline-flex min-h-8 items-center gap-1.5 rounded-full border px-3 text-[13px]",
              mono && "font-mono text-xs",
              on ? "border-ink bg-ink text-on-ink" : "border-field bg-surface text-ink hover:bg-row-hover",
            )}
          >
            {o.label}
            {o.count !== undefined && <span className="text-xs opacity-80">{o.count}</span>}
          </button>
        );
      })}
    </div>
  );
}

/** The banner for a page part that failed to load, with the HTTP reason. */
export function LoadError({ title, detail, onRetry }: { title: string; detail: string; onRetry: () => void }) {
  return (
    <div
      role="alert"
      className="flex flex-wrap items-center justify-between gap-3 rounded-xl border border-badge-danger-fg/25 bg-badge-danger px-[18px] py-3.5 text-badge-danger-fg"
    >
      <div className="flex min-w-0 items-center gap-3">
        <TriangleAlert className="h-[18px] w-[18px] shrink-0" />
        <span className="flex min-w-0 flex-wrap gap-x-1.5">
          <strong className="font-semibold">{title}</strong>
          <span className="break-all">{detail}</span>
        </span>
      </div>
      <button
        type="button"
        onClick={onRetry}
        className="flex min-h-9 items-center gap-1.5 rounded-[9px] border border-badge-danger-fg/30 bg-surface px-3.5"
      >
        <RefreshCw className="h-[15px] w-[15px]" />
        Try again
      </button>
    </div>
  );
}
