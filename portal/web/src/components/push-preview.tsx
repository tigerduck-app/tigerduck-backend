// How a push will look on a locked iPhone and in Android's shade, for the
// push composers. The phone mock-ups keep their own colors in both themes.

import { Bell, BellOff } from "lucide-react";
import { cn } from "@/lib/cn";

function AppMark({ className }: { className?: string }) {
  return (
    <span
      aria-hidden="true"
      className={cn(
        "flex shrink-0 items-center justify-center bg-brand font-bold text-side",
        className,
      )}
    >
      T
    </span>
  );
}

/** The app's mark beside a push's title and body, as the confirm dialogs show it. */
export function PushCard({ title, body, children }: { title: string; body: string; children?: React.ReactNode }) {
  return (
    <div className="flex gap-2.5 rounded-xl border border-line px-3.5 py-3">
      <AppMark className="size-8 rounded-lg text-sm" />
      <div className="flex min-w-0 flex-1 flex-col gap-0.5">
        <span className="break-words font-semibold">{title}</span>
        <span className="break-words text-[13px] text-ink-2">{body}</span>
        {children && <div className="mt-1.5 flex flex-wrap gap-1.5">{children}</div>}
      </div>
    </div>
  );
}

export function IphonePreview({
  title,
  body,
  sound,
  className,
}: {
  title: string;
  body: string;
  /** Shows the bell when given. */
  sound?: boolean;
  className?: string;
}) {
  const today = new Date().toLocaleDateString("en-US", {
    weekday: "long",
    month: "long",
    day: "numeric",
  });
  const Bells = sound ? Bell : BellOff;
  return (
    <div className={cn("flex flex-col gap-1.5", className)}>
      <span className="text-xs text-ink-3">iPhone</span>
      <div className="flex flex-col items-center gap-3 rounded-[18px] bg-[#1f2228] px-3 pb-3.5 pt-4">
        <div className="flex flex-col items-center text-white">
          <span className="text-[11px] text-side-ink">{today}</span>
          <span className="text-[34px] font-light leading-[1.1]">9:41</span>
        </div>
        <div className="flex gap-2.5 self-stretch rounded-[14px] bg-[#edeef0] px-3 py-2.5 text-[#15171a]">
          <AppMark className="size-8 rounded-lg text-sm" />
          <div className="flex min-w-0 flex-1 flex-col">
            <div className="flex justify-between gap-2">
              <span className="truncate text-[13px] font-semibold">{title}</span>
              <span className="flex shrink-0 items-center gap-1 text-[11px] text-[#5b616b]">
                {sound !== undefined && <Bells aria-hidden="true" className="size-3" />}
                now
              </span>
            </div>
            <span className="line-clamp-3 break-words text-[13px] text-[#3a3f47]">{body}</span>
          </div>
        </div>
      </div>
    </div>
  );
}

export function AndroidPreview({
  title,
  body,
  className,
}: {
  title: string;
  body: string;
  className?: string;
}) {
  return (
    <div className={cn("flex flex-col gap-1.5", className)}>
      <span className="text-xs text-ink-3">Android</span>
      <div className="flex flex-col gap-1 rounded-[18px] border border-line bg-surface px-3.5 py-3">
        <div className="flex items-center gap-1.5 text-xs text-ink-3">
          <AppMark className="size-4 rounded-full text-[10px]" />
          TigerDuck · now
        </div>
        <span className="truncate font-medium">{title}</span>
        <span className="line-clamp-3 break-words text-[13px] text-ink-2">{body}</span>
      </div>
    </div>
  );
}
