// This week's classes for one user: the term's course meetings on a
// weekday-by-period grid, with the dates the user skipped or the school
// cancelled struck through and the school's coming room changes tagged.

import { cn } from "@/lib/cn";
import type { UserDetail } from "@/types/api";
import { changeFor } from "../moodle/Timetable";

// NTUST periods and their start times, as in server/config.py
// `course_period_start_times`.
const PERIODS: [string, string][] = [
  ["1", "08:10"],
  ["2", "09:10"],
  ["3", "10:20"],
  ["4", "11:20"],
  ["5", "12:20"],
  ["6", "13:20"],
  ["7", "14:20"],
  ["8", "15:30"],
  ["9", "16:30"],
  ["10", "17:30"],
  ["A", "18:25"],
  ["B", "19:20"],
  ["C", "20:15"],
  ["D", "21:10"],
];
const DAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"];

type Block = {
  name: string;
  code: string;
  room: string;
  day: number;
  from: number;
  to: number;
  cancelled: boolean;
  tag: string | null;
};

export function isoDate(start: string, offset: number): string {
  const d = new Date(`${start}T00:00:00Z`);
  d.setUTCDate(d.getUTCDate() + offset);
  return d.toISOString().slice(0, 10);
}

/** Each meeting split into runs of consecutive periods. */
function blocks({ courses, changes, week_start: weekStart }: UserDetail): Block[] {
  const out: Block[] = [];
  for (const c of courses) {
    for (const entry of c.schedule) {
      const day = entry.day;
      if (typeof day !== "number" || day < 1 || day > 7 || !Array.isArray(entry.periods)) continue;
      const rows = [...new Set(entry.periods.map((p) => PERIODS.findIndex(([id]) => id === String(p))))]
        .filter((i) => i >= 0)
        .sort((a, b) => a - b);
      const cancelled = c.skipped.includes(isoDate(weekStart, day - 1));
      rows.forEach((row, i) => {
        const last = out[out.length - 1];
        if (i > 0 && last.to === row - 1) last.to = row;
        else
          out.push({ name: c.name, code: c.course_no ?? "", room: c.classroom ?? "", day, from: row, to: row, cancelled, tag: null });
      });
    }
  }
  const rooms = changes.filter((c) => c.kind === "room");
  const cancels = changes.filter((c) => c.kind === "cancelled");
  return out.map((b) => {
    const cancelled = b.cancelled || changeFor(b, cancels)?.date === isoDate(weekStart, b.day - 1);
    return { ...b, cancelled, tag: cancelled ? null : (changeFor(b, rooms)?.label ?? null) };
  });
}

export function Timetable({ user }: { user: UserDetail }) {
  const weekStart = user.week_start;
  const all = blocks(user);
  // Weekdays and periods 1 to 10 always; a weekend day or evening period only when a class meets then.
  const dayCount = Math.max(5, ...all.map((b) => b.day));
  const periodCount = Math.max(10, ...all.map((b) => b.to + 1));
  const todayIndex = Math.round(
    (Date.parse(new Date().toLocaleDateString("en-CA", { timeZone: "Asia/Taipei" })) - Date.parse(weekStart)) /
      86_400_000,
  );
  const meetings = all.length;
  const cancelled = all.filter((b) => b.cancelled).map((b) => b.name);
  const moved = all.filter((b) => b.tag).map((b) => `${b.name} ${b.tag}`);

  return (
    <div className="overflow-x-auto">
      <div
        role="img"
        aria-label={`Timetable for this week with ${meetings} class meetings${cancelled.length ? `; cancelled: ${[...new Set(cancelled)].join(", ")}` : ""}${moved.length ? `; room changes: ${moved.join(", ")}` : ""}`}
        className="grid min-w-[820px] gap-[3px] phone:min-w-[560px] phone:[--tt-col:100px] phone:[--tt-head:32px]"
        style={{
          gridTemplateColumns: `var(--tt-head, 44px) repeat(${dayCount}, minmax(var(--tt-col, 150px), 1fr))`,
          gridTemplateRows: `22px repeat(${periodCount}, 30px)`,
        }}
      >
        {DAYS.slice(0, dayCount).map((d, i) => (
          <span
            key={d}
            className={cn("pl-1 text-xs", i === todayIndex ? "font-semibold text-ink" : "font-medium text-ink-3")}
            style={{ gridColumn: i + 2, gridRow: 1 }}
          >
            {d}
            {i === todayIndex && " · today"}
          </span>
        ))}
        {PERIODS.slice(0, periodCount).map(([id, time], p) => (
          <span
            key={id}
            className="flex items-center text-[10px] leading-tight tabular-nums text-ink-3"
            style={{ gridColumn: 1, gridRow: p + 2 }}
          >
            {id} · {time}
          </span>
        ))}
        {PERIODS.slice(0, periodCount).flatMap((_, p) =>
          DAYS.slice(0, dayCount).map((_, d) => (
            <span
              key={`${p}-${d}`}
              className={cn("rounded", d === todayIndex ? "bg-selected" : "bg-row-hover")}
              style={{ gridColumn: d + 2, gridRow: p + 2 }}
            />
          )),
        )}
        {all.map((b, i) => (
          <div
            key={i}
            className={cn(
              "z-[1] flex flex-col gap-px overflow-hidden rounded-md px-2 py-[5px]",
              b.cancelled
                ? "bg-surface text-dot-off shadow-[inset_0_0_0_1.5px_var(--color-line)]"
                : "bg-badge-info text-ink",
            )}
            style={{ gridColumn: b.day + 1, gridRow: `${b.from + 2} / span ${b.to - b.from + 1}` }}
          >
            <span className={cn("truncate text-xs font-semibold", b.cancelled && "line-through")}>{b.name}</span>
            <span className={cn("font-mono text-[11px]", b.cancelled && "line-through")}>{b.room}</span>
            {b.cancelled && (
              <span className="mt-auto self-start rounded-full bg-badge-danger px-1.5 py-px text-[10px] font-semibold text-badge-danger-fg">
                Cancelled
              </span>
            )}
            {b.tag && (
              <span
                title={b.tag}
                className="mt-auto max-w-full self-start truncate rounded-full bg-surface px-1.5 py-px text-[10px] font-semibold text-badge-info-fg"
              >
                {b.tag}
              </span>
            )}
          </div>
        ))}
      </div>
    </div>
  );
}
