// The Status page's bar charts: a tile's sparkline and a hoverable column chart.

import * as React from "react";
import { cn } from "@/lib/cn";
import { num } from "./format";

export function Spark({ values }: { values: number[] }) {
  const max = Math.max(1, ...values);
  return (
    <div aria-hidden="true" className="mt-1.5 flex h-7 items-end gap-0.5">
      {values.map((v, i) => (
        <span
          key={i}
          className="flex-1 rounded-t-[2px] bg-chart-1/35"
          style={{ height: `${Math.max(8, (v / max) * 100)}%` }}
        />
      ))}
    </div>
  );
}

// A round step, at least 1, so four of them reach `max`.
function step(max: number): number {
  const raw = max / 4;
  if (raw <= 1) return 1;
  const mag = 10 ** Math.floor(Math.log10(raw));
  const f = raw / mag;
  return (f <= 1 ? 1 : f <= 2 ? 2 : f <= 2.5 ? 2.5 : f <= 5 ? 5 : 10) * mag;
}

export type Point = { label: string; value: number };

/**
 * Columns with a hover tooltip. `axis` adds the value axis and gridlines;
 * without it the columns stand on a baseline. The x axis names the first,
 * middle and last point.
 */
export function ColumnChart({
  points,
  axis = false,
  aria,
  tip,
  className,
}: {
  points: Point[];
  axis?: boolean;
  aria: string;
  tip: (p: Point) => [string, string];
  className?: string;
}) {
  const [hover, setHover] = React.useState<number | null>(null);
  const max = Math.max(0, ...points.map((p) => p.value));
  const top = axis ? step(max) * 4 : Math.max(1, max);
  const ticks = [4, 3, 2, 1, 0].map((i) => (top / 4) * i);
  const shown = hover !== null ? points[hover] : null;
  const marks = points.length
    ? [points[0], points[Math.floor(points.length / 2)], points[points.length - 1]]
    : [];
  const pct = (v: number) => `${((v / top) * 100).toFixed(1)}%`;

  return (
    <div className="flex gap-2">
      {axis && (
        <div
          aria-hidden="true"
          className="flex h-[180px] flex-col justify-between pb-[18px] text-right text-[11px] tabular-nums text-ink-3"
        >
          {ticks.map((t) => (
            <span key={t}>{num(t)}</span>
          ))}
        </div>
      )}
      <div className="relative min-w-0 flex-1">
        {axis && (
          <div aria-hidden="true" className="absolute inset-x-0 bottom-[25px] top-[7px] flex flex-col justify-between">
            {ticks.map((t) => (
              <span key={t} className="h-px bg-divider" />
            ))}
          </div>
        )}
        <div
          role="img"
          aria-label={aria}
          className={cn("relative mt-[7px] flex h-[162px] items-end", !axis && "border-b border-line", className)}
        >
          {points.map((p, i) => (
            <span
              key={p.label}
              onMouseEnter={() => setHover(i)}
              onMouseLeave={() => setHover(null)}
              className="flex h-full min-w-0 flex-1 items-end"
            >
              <span
                className={cn("w-full rounded-t-[4px]", hover === i ? "bg-[#1c5cab]" : "bg-chart-1")}
                style={{ height: pct(p.value) }}
              />
            </span>
          ))}
          {shown && (
            <div
              className="pointer-events-none absolute z-[5] flex -translate-x-1/2 gap-2 whitespace-nowrap rounded-lg bg-[#15171A] px-2.5 py-1.5 text-xs text-white"
              style={{
                bottom: `calc(${pct(shown.value)} + 8px)`,
                // Kept inside the chart so an end column's tip cannot widen the page.
                left: `clamp(64px, ${((hover! + 0.5) / points.length) * 100}%, calc(100% - 64px))`,
              }}
            >
              <span className="font-semibold">{tip(shown)[0]}</span>
              <span className="text-[#C4C9D1]">{tip(shown)[1]}</span>
            </div>
          )}
        </div>
        <div aria-hidden="true" className="flex h-[18px] justify-between pt-1 text-[11px] text-ink-3">
          {marks.map((p, i) => (
            <span key={i}>{p.label}</span>
          ))}
        </div>
      </div>
    </div>
  );
}
