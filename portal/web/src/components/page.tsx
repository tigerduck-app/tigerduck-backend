// Building blocks of the redesigned pages: header, panels, pills, dialogs.

import * as React from "react";
import * as DialogPrimitive from "@radix-ui/react-dialog";
import { TriangleAlert, X, type LucideIcon } from "lucide-react";
import { Button } from "@/components/ui/button";
import { cn } from "@/lib/cn";

export const fieldClass =
  "h-10 w-full rounded-[9px] border border-field bg-surface px-2.5 text-sm text-ink placeholder:text-ink-3 disabled:opacity-60";

export function PageHeading({
  group,
  title,
  meta,
  actions,
}: {
  group: string;
  title: React.ReactNode;
  meta?: React.ReactNode;
  actions?: React.ReactNode;
}) {
  return (
    <header className="flex flex-wrap items-end justify-between gap-4">
      <div className="flex min-w-0 flex-col gap-1">
        <span className="text-[13px] text-ink-3">{group}</span>
        <h1 className="text-[28px] font-semibold leading-tight tracking-[-0.01em] phone:text-[22px]">
          {title}
        </h1>
        {meta && <span className="font-mono text-[13px] text-ink-3">{meta}</span>}
      </div>
      {actions && <div className="flex flex-wrap items-center gap-2">{actions}</div>}
    </header>
  );
}

/** A white card section; `title` gets a header row with `aside` at its right. */
export function Panel({
  title,
  aside,
  children,
  className,
}: {
  title?: React.ReactNode;
  aside?: React.ReactNode;
  children: React.ReactNode;
  className?: string;
}) {
  const id = React.useId();
  return (
    <section
      aria-labelledby={title ? id : undefined}
      className={cn("min-w-0 rounded-[14px] border border-line bg-surface", className)}
    >
      {title && (
        <div className="flex flex-wrap items-center justify-between gap-3 border-b border-line px-6 py-4 phone:px-4">
          <h2 id={id} className="text-[15px] font-semibold">
            {title}
          </h2>
          {aside}
        </div>
      )}
      {children}
    </section>
  );
}

export type Tone = "neutral" | "info" | "success" | "warning" | "danger" | "violet" | "orange";
export type DotTone = "ok" | "warn" | "bad" | "off";

const TONE: Record<Tone, string> = {
  neutral: "bg-badge-neutral text-badge-neutral-fg",
  info: "bg-badge-info text-badge-info-fg",
  success: "bg-badge-success text-badge-success-fg",
  warning: "bg-badge-warning text-badge-warning-fg",
  danger: "bg-badge-danger text-badge-danger-fg",
  violet: "bg-badge-violet text-badge-violet-fg",
  orange: "bg-badge-orange text-badge-orange-fg",
};

const DOT: Record<DotTone, string> = {
  ok: "bg-dot-ok",
  warn: "bg-dot-warn",
  bad: "bg-dot-bad",
  off: "bg-dot-off",
};

export function Dot({ tone, className }: { tone: DotTone; className?: string }) {
  return (
    <span
      aria-hidden="true"
      className={cn("size-2 shrink-0 rounded-full", DOT[tone], className)}
    />
  );
}

/** A badge always carries a word, never color alone. */
export function Pill({
  tone = "neutral",
  dot,
  title,
  className,
  children,
}: {
  tone?: Tone;
  dot?: DotTone;
  title?: string;
  className?: string;
  children: React.ReactNode;
}) {
  return (
    <span
      title={title}
      className={cn(
        "inline-flex items-center gap-1.5 whitespace-nowrap rounded-full px-[9px] py-[3px] text-xs font-semibold",
        TONE[tone],
        className,
      )}
    >
      {dot && <Dot tone={dot} className="size-[7px]" />}
      {children}
    </span>
  );
}

export function EmptyState({
  icon: Icon,
  title,
  line,
  action,
}: {
  icon: LucideIcon;
  title: string;
  line?: string;
  action?: React.ReactNode;
}) {
  return (
    <div className="flex flex-col items-center gap-2.5 px-6 py-12 text-center">
      <div
        aria-hidden="true"
        className="flex size-11 items-center justify-center rounded-full bg-ground text-ink-3"
      >
        <Icon className="h-5 w-5" />
      </div>
      <span className="text-[15px] font-semibold">{title}</span>
      {line && <span className="text-[13px] text-ink-3">{line}</span>}
      {action && <div className="mt-1">{action}</div>}
    </div>
  );
}

export function ErrorAlert({
  children,
  onRetry,
  className,
}: {
  children: React.ReactNode;
  onRetry?: () => void;
  className?: string;
}) {
  return (
    <div
      role="alert"
      className={cn(
        "flex items-start gap-2.5 rounded-[10px] bg-badge-danger px-3.5 py-3 text-[13px] text-badge-danger-fg",
        className,
      )}
    >
      <TriangleAlert className="mt-px h-4 w-4 shrink-0" />
      <div className="min-w-0 flex-1 break-words">{children}</div>
      {onRetry && (
        <button
          type="button"
          onClick={onRetry}
          className="-my-1 min-h-8 shrink-0 rounded-lg border border-badge-danger-fg/30 bg-surface px-3 text-badge-danger-fg"
        >
          Retry
        </button>
      )}
    </div>
  );
}

export function Switch({
  checked,
  onCheckedChange,
  disabled,
  label,
  children,
  className,
}: {
  checked: boolean;
  onCheckedChange: (next: boolean) => void;
  disabled?: boolean;
  /** Accessible name when there is no visible text. */
  label?: string;
  children?: React.ReactNode;
  className?: string;
}) {
  return (
    <button
      type="button"
      role="switch"
      aria-checked={checked}
      aria-label={label}
      disabled={disabled}
      onClick={() => onCheckedChange(!checked)}
      className={cn(
        "inline-flex shrink-0 items-center gap-2 rounded-[9px] text-[13px] text-ink disabled:cursor-not-allowed disabled:opacity-50",
        className,
      )}
    >
      <span
        aria-hidden="true"
        className={cn(
          "relative h-6 w-10 shrink-0 rounded-full transition-colors motion-reduce:transition-none",
          checked ? "bg-dot-ok" : "bg-disabled",
        )}
      >
        <span
          className={cn(
            "absolute left-[3px] top-[3px] size-[18px] rounded-full bg-white shadow-[0_1px_2px_rgba(0,0,0,0.2)] transition-transform motion-reduce:transition-none",
            checked && "translate-x-4",
          )}
        />
      </span>
      {children}
    </button>
  );
}

/** The 36px badge some dialogs show before their title. */
export function DialogIcon({
  icon: Icon,
  danger = false,
  round = false,
}: {
  icon: LucideIcon;
  danger?: boolean;
  round?: boolean;
}) {
  return (
    <span
      aria-hidden="true"
      className={cn(
        "flex size-9 shrink-0 items-center justify-center",
        round ? "rounded-full" : "rounded-[10px]",
        danger ? "bg-badge-danger text-badge-danger-fg" : "bg-ground text-ink-2",
      )}
    >
      <Icon className="h-[18px] w-[18px]" />
    </span>
  );
}

/** The redesign's dialog: title and meta line, a body, and a footer of actions. */
export function FormDialog({
  open,
  onOpenChange,
  icon,
  title,
  meta,
  width = 640,
  footer,
  children,
}: {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  /** A `DialogIcon` before the title. */
  icon?: React.ReactNode;
  title: React.ReactNode;
  meta?: React.ReactNode;
  width?: number;
  /** Leave out for a dialog whose rows are its actions. */
  footer?: React.ReactNode;
  /** Leave out for a confirm that is only a question. */
  children?: React.ReactNode;
}) {
  return (
    <DialogPrimitive.Root open={open} onOpenChange={onOpenChange}>
      <DialogPrimitive.Portal>
        <DialogPrimitive.Overlay className="fixed inset-0 z-50 bg-[rgba(20,22,26,0.48)] animate-fade-in" />
        <DialogPrimitive.Content
          {...(meta ? {} : { "aria-describedby": undefined })}
          style={{ maxWidth: width }}
          className="fixed left-1/2 top-[72px] z-50 flex max-h-[calc(100vh-96px)] w-[calc(100%-48px)] -translate-x-1/2 flex-col overflow-hidden rounded-2xl bg-surface font-sans text-sm leading-[1.45] text-ink shadow-[0_24px_64px_rgba(20,22,26,0.24)] animate-fade-in max-[720px]:top-4 max-[720px]:max-h-[calc(100vh-32px)] max-[720px]:w-[calc(100%-32px)]"
        >
          <div
            className={cn(
              "flex gap-3 px-6 pb-4 pt-5 max-[720px]:px-4",
              meta ? "items-start" : "items-center",
            )}
          >
            {icon}
            <div className="flex min-w-0 flex-col gap-0.5">
              <DialogPrimitive.Title className="break-words text-lg font-semibold">
                {title}
              </DialogPrimitive.Title>
              {meta && (
                <DialogPrimitive.Description className="break-words text-[13px] text-ink-3">
                  {meta}
                </DialogPrimitive.Description>
              )}
            </div>
            <DialogPrimitive.Close asChild>
              <Button variant="quiet" size="icon" aria-label="Close" className="ml-auto shrink-0 rounded-lg">
                <X className="!size-[18px]" />
              </Button>
            </DialogPrimitive.Close>
          </div>
          {children && (
            <div className="flex min-h-0 flex-col gap-4 overflow-y-auto px-6 pb-5 *:shrink-0 max-[720px]:px-4">
              {children}
            </div>
          )}
          {footer && (
            <div className="flex flex-wrap justify-end gap-2 border-t border-line px-6 py-3.5 max-[720px]:px-4">
              {footer}
            </div>
          )}
        </DialogPrimitive.Content>
      </DialogPrimitive.Portal>
    </DialogPrimitive.Root>
  );
}
