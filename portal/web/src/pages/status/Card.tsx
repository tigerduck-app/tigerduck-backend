import * as React from "react";
import { cn } from "@/lib/cn";

/** A white card with its title inline, for charts and timetables. */
export function Card({
  title,
  aside,
  className,
  children,
}: {
  title: string;
  aside?: React.ReactNode;
  className?: string;
  children: React.ReactNode;
}) {
  const id = React.useId();
  return (
    <section
      aria-labelledby={id}
      className={cn(
        "flex min-w-0 flex-col gap-3.5 rounded-[14px] border border-line bg-surface px-6 pb-5 pt-4 phone:px-4",
        className,
      )}
    >
      <div className="flex min-h-[38px] flex-wrap items-center justify-between gap-2">
        <h2 id={id} className="text-[15px] font-semibold">
          {title}
        </h2>
        {aside}
      </div>
      {children}
    </section>
  );
}
