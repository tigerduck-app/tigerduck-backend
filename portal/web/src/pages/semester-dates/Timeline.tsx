import { useState } from "react";
import { cn } from "@/lib/cn";
import {
  daysBetween,
  rangeText,
  termLabel,
  weeks,
  type Holiday,
  type Term,
} from "@/lib/academic";

const MONTHS = ["Aug", "Sep", "Oct", "Nov", "Dec", "Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul"];

/** Academic year `ay` (Aug 1 to Jul 31) as a bar per term and holiday. */
export function Timeline({
  ay,
  terms,
  holidays,
  today,
  onTerm,
  onHoliday,
}: {
  ay: number;
  terms: readonly Term[];
  holidays: readonly Holiday[];
  today: string;
  onTerm: (t: Term) => void;
  onHoliday: (h: Holiday) => void;
}) {
  const [hover, setHover] = useState<string | null>(null);
  const a0 = `${ay + 1911}-08-01`;
  const a1 = `${ay + 1912}-07-31`;
  const span = daysBetween(a0, a1) + 1;
  const pct = (n: number) => `${((n / span) * 100).toFixed(2)}%`;
  const clip = (from: string, to: string) => {
    const l = Math.max(0, daysBetween(a0, from));
    const r = Math.min(span, daysBetween(a0, to) + 1);
    return r > l ? { l, w: r - l } : null;
  };
  let tip: { title: string; range: string; at: number; top: number } | null = null;

  const termBars = terms.flatMap((t) => {
    const c = clip(t.start_date, t.end_date);
    if (!c) return [];
    const id = `t-${t.code}`;
    const range = rangeText(t.start_date, t.end_date);
    if (hover === id) {
      tip = { title: termLabel(t.code), range: `${range} · ${weeks(t.start_date, t.end_date)} weeks`, at: c.l + c.w / 2, top: 48 };
    }
    return [
      <button
        key={id}
        type="button"
        aria-label={`${termLabel(t.code)}, ${range}`}
        onClick={() => onTerm(t)}
        onMouseEnter={() => setHover(id)}
        onMouseLeave={() => setHover(null)}
        onFocus={() => setHover(id)}
        onBlur={() => setHover(null)}
        style={{ left: pct(c.l), width: pct(c.w) }}
        className={cn(
          "absolute top-12 flex h-10 items-center rounded-lg text-left text-on-ink",
          hover === id ? "bg-link-hover" : "bg-link",
        )}
      >
        <span className="truncate px-2.5 text-[13px] font-semibold">{termLabel(t.code)}</span>
      </button>,
    ];
  });

  const holidayBars = holidays.flatMap((h) => {
    const c = clip(h.start_date, h.end_date);
    if (!c) return [];
    const id = `h-${h.id}`;
    const days = daysBetween(h.start_date, h.end_date) + 1;
    const range = rangeText(h.start_date, h.end_date);
    if (hover === id) {
      tip = { title: `${h.name_zh} ${h.name_en}`, range: `${range} · ${days} ${days === 1 ? "day" : "days"}`, at: c.l + c.w / 2, top: 98 };
    }
    return [
      <button
        key={id}
        type="button"
        aria-label={`${h.name_zh} ${h.name_en}, ${range}`}
        onClick={() => onHoliday(h)}
        onMouseEnter={() => setHover(id)}
        onMouseLeave={() => setHover(null)}
        onFocus={() => setHover(id)}
        onBlur={() => setHover(null)}
        style={{ left: pct(c.l), width: pct(c.w) }}
        className={cn("absolute top-[98px] h-[26px] min-w-2 rounded-[5px]", hover === id ? "bg-dot-warn" : "bg-brand")}
      />,
    ];
  });

  const shown = tip as { title: string; range: string; at: number; top: number } | null;
  const todayAt = today >= a0 && today <= a1 ? pct(daysBetween(a0, today)) : null;

  return (
    <div className="overflow-x-auto">
      <div className="relative h-[168px] min-w-[820px]">
        {MONTHS.map((label, i) => {
          const y = ay + 1911 + (i < 5 ? 0 : 1);
          const first = `${y}-${String(((7 + i) % 12) + 1).padStart(2, "0")}-01`;
          return (
            <div
              key={label}
              style={{ left: pct(daysBetween(a0, first)) }}
              className="absolute bottom-0 top-10 w-px bg-divider"
            >
              <span className="absolute bottom-0 left-1.5 text-[11px] text-ink-3">{label}</span>
            </div>
          );
        })}
        {termBars}
        {holidayBars}
        {todayAt && (
          <>
            <div aria-hidden="true" style={{ left: todayAt }} className="absolute bottom-[18px] top-10 w-0.5 bg-ink" />
            <span style={{ left: todayAt }} className="absolute top-[130px] -translate-x-1/2 text-[11px] font-semibold text-ink">
              Today
            </span>
          </>
        )}
        {shown && (
          <div
            role="tooltip"
            style={{ left: pct(shown.at), top: shown.top - 6 }}
            className="pointer-events-none absolute z-[5] flex -translate-x-1/2 -translate-y-full flex-col gap-px whitespace-nowrap rounded-[10px] bg-ink px-3 py-2 text-[13px] text-on-ink shadow-[0_8px_24px_rgba(20,22,26,0.24)]"
          >
            <span className="font-semibold">{shown.title}</span>
            <span className="text-xs tabular-nums opacity-75">{shown.range}</span>
          </div>
        )}
      </div>
    </div>
  );
}
