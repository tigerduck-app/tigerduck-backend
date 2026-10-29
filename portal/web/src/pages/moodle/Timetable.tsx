// A student's week of classes in one term, with the school's coming changes
// to them tagged on each meeting. Phones get one day at a time.

import { useRef, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { Link } from "react-router-dom";
import { CalendarX } from "lucide-react";
import { api } from "@/lib/api";
import { cn } from "@/lib/cn";
import { monthDay } from "@/lib/format";
import { Skeleton } from "@/components/ui/skeleton";
import { LoadError, Segmented } from "./controls";
import { errorText, termLabel } from "./format";
import type { CourseChange, TimetableResponse } from "./types";

// server/config.py `course_period_start_times`.
const PERIODS: [string, string][] = [
  ["1", "08:10"], ["2", "09:10"], ["3", "10:20"], ["4", "11:20"], ["5", "12:20"],
  ["6", "13:20"], ["7", "14:20"], ["8", "15:30"], ["9", "16:30"], ["10", "17:30"],
  ["A", "18:25"], ["B", "19:20"], ["C", "20:15"], ["D", "21:10"],
];
const DAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"];
const TAG: Record<CourseChange["kind"], string> = {
  room: "bg-badge-info text-badge-info-fg",
  cancelled: "bg-badge-danger text-badge-danger-fg",
  start: "bg-badge-orange text-badge-orange-fg",
  end: "bg-badge-orange text-badge-orange-fg",
  note: "bg-badge-violet text-badge-violet-fg",
};

type Block = {
  key: string;
  name: string;
  code: string;
  room: string;
  color: string;
  /** ISO weekday, 1 is Monday. */
  day: number;
  /** Rows in PERIODS. */
  from: number;
  to: number;
  change: { kind: CourseChange["kind"]; label: string; date: string } | null;
};

const taipeiToday = () => new Date().toLocaleDateString("en-CA", { timeZone: "Asia/Taipei" });

function isoDay(date: string): number {
  return new Date(`${date}T00:00:00Z`).getUTCDay() || 7;
}

/** The first coming change that reaches this meeting: a class day of the change on its weekday, in one of its periods.
 * On a day with several, a cancellation wins, since the class does not meet. */
export function changeFor(
  block: Pick<Block, "code" | "day" | "from" | "to">,
  changes: CourseChange[],
): Block["change"] {
  const periods = PERIODS.slice(block.from, block.to + 1).map(([id]) => id);
  let best: { date: string; c: CourseChange } | null = null;
  for (const c of changes) {
    if (c.course_no !== block.code) continue;
    if (c.periods && !c.periods.some((p) => periods.includes(String(p)))) continue;
    const date = c.class_days.find((d) => isoDay(d) === block.day);
    if (!date || (best && (date > best.date || (date === best.date && best.c.kind === "cancelled")))) continue;
    if (!best || date < best.date || c.kind === "cancelled") best = { date, c };
  }
  if (!best) return null;
  const { c, date } = best;
  const what =
    c.kind === "cancelled" ? "Cancelled"
    : c.kind === "room" ? c.room ?? ""
    : c.kind === "start" ? `starts ${c.start_time ?? ""}`
    : c.kind === "end" ? `ends ${c.end_time ?? ""}`
    : c.note_zh ?? c.note_en ?? "";
  const day = monthDay(`${date}T00:00:00Z`, true);
  return { kind: c.kind, label: `${c.mode === "range" ? `From ${day}` : day}: ${what}`, date };
}

const sameChange = (a: Block["change"], b: Block["change"]) =>
  a === b || (a !== null && b !== null && a.kind === b.kind && a.label === b.label && a.date === b.date);

function blocksOf(data: TimetableResponse): Block[] {
  const out: Block[] = [];
  data.courses.forEach((c, ci) => {
    c.schedule.forEach((entry, ei) => {
      const day = entry.day;
      if (typeof day !== "number" || day < 1 || day > 7 || !Array.isArray(entry.periods)) return;
      const code = c.course_no ?? "";
      const rows = [...new Set(entry.periods.map((p) => PERIODS.findIndex(([id]) => id === String(p))))]
        .filter((i) => i >= 0)
        .sort((a, b) => a - b);
      rows.forEach((row, i) => {
        // Period by period, since a change may reach only some of a meeting's periods.
        const change = changeFor({ code, day, from: row, to: row }, data.changes);
        const last = out[out.length - 1];
        if (i > 0 && last.to === row - 1 && sameChange(last.change, change)) last.to = row;
        else
          out.push({
            key: `${ci}-${ei}-${row}`,
            name: c.name,
            code,
            room: c.classroom ?? "",
            color: c.color,
            day,
            from: row,
            to: row,
            change,
          });
      });
    });
  });
  return out;
}

export const changesLink = (term: string | null | undefined) =>
  term ? `/course-changes?term=${encodeURIComponent(term)}` : "/course-changes";

const periodText = (b: Block) =>
  b.from === b.to ? `P${PERIODS[b.from][0]}` : `P${PERIODS[b.from][0]}–${PERIODS[b.to][0]}`;

function Tag({ change }: { change: NonNullable<Block["change"]> }) {
  return (
    <span
      className={cn(
        "max-w-full self-start truncate rounded-full px-[7px] py-px text-[11px] font-semibold",
        TAG[change.kind],
      )}
    >
      {change.label}
    </span>
  );
}

export function Timetable({ studentId }: { studentId: string }) {
  const [term, setTerm] = useState<string | null>(null);
  const [pickedDay, setPickedDay] = useState<number | null>(null);
  const q = useQuery({
    queryKey: ["moodle", "timetable", studentId, term],
    queryFn: () =>
      api<TimetableResponse>(
        `/api/moodle/timetable?student_id=${encodeURIComponent(studentId)}${term ? `&term=${term}` : ""}`,
      ),
    placeholderData: (prev) => prev,
  });
  // The terms last answered keep the toggle up, so a failed term can be left; only the chosen term's own answer draws the grid.
  const terms = useRef<string[]>([]);
  if (q.data) terms.current = q.data.terms;
  const data = q.isPlaceholderData || q.isError ? undefined : q.data;
  const today = taipeiToday();
  const current = !!data && data.term === data.current_term;
  const todayDay = current ? isoDay(today) : -1;
  const blocks = data ? blocksOf(data) : [];
  const dayCount = Math.max(5, ...blocks.map((b) => b.day));
  const periodCount = Math.max(10, ...blocks.map((b) => b.to + 1));
  const day = pickedDay ?? (todayDay >= 1 && todayDay <= dayCount ? todayDay : 1);
  const dayBlocks = blocks.filter((b) => b.day === day).sort((a, b) => a.from - b.from);
  const codes = new Set(data?.courses.map((c) => c.course_no ?? c.name));
  const periods = blocks.reduce((n, b) => n + b.to - b.from + 1, 0);
  const title = (b: Block) =>
    `${b.name} ${b.code}, ${DAYS[b.day - 1]} periods ${PERIODS[b.from][0]}–${PERIODS[b.to][0]}${b.room ? `, ${b.room}` : ""}${b.change ? `, ${b.change.label}` : ""}`;

  return (
    <section
      aria-labelledby="tt-h"
      className="flex flex-col gap-3.5 rounded-[14px] border border-line bg-surface px-6 pb-5 pt-4 phone:px-4"
    >
      <div className="flex flex-wrap items-center gap-x-4 gap-y-2.5">
        <h2 id="tt-h" className="text-[15px] font-semibold">
          Timetable
        </h2>
        {terms.current.length > 0 && (
          <Segmented
            label="Term"
            value={term ?? q.data?.term ?? ""}
            onChange={(t) => {
              setTerm(t);
              setPickedDay(null);
            }}
            options={terms.current.map((t) => ({ value: t, label: termLabel(t) }))}
            className="max-w-full overflow-x-auto"
            itemClassName="font-mono"
          />
        )}
        {blocks.length > 0 && (
          <span className="whitespace-nowrap text-[13px] text-ink-3">
            {codes.size} courses · {periods} periods a week
          </span>
        )}
        <Link
          to={changesLink(term ?? q.data?.term)}
          className="ml-auto whitespace-nowrap text-[13px] text-link no-underline hover:text-link-hover"
        >
          Course changes
        </Link>
      </div>

      {!data && !q.isError && <Skeleton className="h-[300px] rounded-xl" />}
      {q.isError && (
        <LoadError title="Couldn’t load the timetable" detail={errorText(q.error)} onRetry={() => void q.refetch()} />
      )}
      {data && blocks.length === 0 && (
        <div className="flex flex-col items-center gap-2.5 rounded-xl bg-row-hover px-6 py-12">
          <div aria-hidden="true" className="flex size-11 items-center justify-center rounded-full bg-surface text-ink-3">
            <CalendarX className="h-5 w-5" />
          </div>
          <span className="text-[15px] font-semibold">No courses in {data.term ? termLabel(data.term) : "—"}</span>
        </div>
      )}
      {data && blocks.length > 0 && (
        <>
          <div className="overflow-x-auto phone:hidden">
            <div
              role="img"
              aria-label={`Timetable for ${termLabel(data.term ?? "")}: ${blocks
                .map((b) => `${DAYS[b.day - 1]} periods ${PERIODS[b.from][0]} to ${PERIODS[b.to][0]} ${b.name}${b.room ? ` in ${b.room}` : ""}${b.change ? ` (${b.change.label})` : ""}`)
                .join("; ")}`}
              className="grid min-w-[840px] gap-[3px] tablet:min-w-0"
              style={{
                gridTemplateColumns: `64px repeat(${dayCount}, minmax(0, 1fr))`,
                gridTemplateRows: `26px repeat(${periodCount}, 44px)`,
              }}
            >
              {DAYS.slice(0, dayCount).map((d, i) => (
                <span
                  key={d}
                  className={cn(
                    "self-center whitespace-nowrap pl-1.5 text-xs",
                    i + 1 === todayDay ? "font-semibold text-ink" : "font-medium text-ink-3",
                  )}
                  style={{ gridColumn: i + 2, gridRow: 1 }}
                >
                  {d}
                  {i + 1 === todayDay && " · today"}
                </span>
              ))}
              {PERIODS.slice(0, periodCount).map(([id, time], p) => (
                <span
                  key={id}
                  className="flex flex-col justify-center text-[11px] leading-tight tabular-nums text-ink-3"
                  style={{ gridColumn: 1, gridRow: p + 2 }}
                >
                  <span className="text-xs font-semibold text-ink-2">{id}</span>
                  {time}
                </span>
              ))}
              {PERIODS.slice(0, periodCount).flatMap((_, p) =>
                DAYS.slice(0, dayCount).map((_, d) => (
                  <span
                    key={`${p}-${d}`}
                    className={cn("rounded", d + 1 === todayDay ? "bg-selected" : "bg-row-hover")}
                    style={{ gridColumn: d + 2, gridRow: p + 2 }}
                  />
                )),
              )}
              {blocks.map((b) => (
                <div
                  key={b.key}
                  title={title(b)}
                  className={cn(
                    "z-[1] flex min-w-0 flex-col gap-px overflow-hidden rounded-md border-l-4 bg-surface py-[5px] pl-2.5 pr-2 shadow-[inset_0_0_0_1px_var(--color-line)]",
                    b.change?.kind === "cancelled" ? "text-ink-3" : "text-ink",
                  )}
                  style={{
                    gridColumn: b.day + 1,
                    gridRow: `${b.from + 2} / span ${b.to - b.from + 1}`,
                    borderLeftColor: b.color,
                  }}
                >
                  <span className="truncate text-[13px] font-semibold">{b.name}</span>
                  <span className="truncate font-mono text-[11px] text-ink-2">
                    {[b.code, b.room].filter(Boolean).join(" · ")}
                  </span>
                  {b.change && <span className="mt-auto flex"><Tag change={b.change} /></span>}
                </div>
              ))}
            </div>
          </div>

          <div className="hidden flex-col gap-2.5 phone:flex">
            <Segmented
              label="Day"
              value={String(day)}
              onChange={(d) => setPickedDay(Number(d))}
              options={DAYS.slice(0, dayCount).map((d, i) => ({ value: String(i + 1), label: d }))}
              itemClassName="min-h-9 flex-1"
            />
            {dayBlocks.length > 0 ? (
              <ol aria-label={`${DAYS[day - 1]} classes`} className="flex flex-col gap-2">
                {dayBlocks.map((b) => (
                  <li
                    key={b.key}
                    className={cn(
                      "flex gap-3 rounded-[10px] border-l-4 bg-surface px-3 py-2.5 shadow-[inset_0_0_0_1px_var(--color-line)]",
                      b.change?.kind === "cancelled" && "text-ink-3",
                    )}
                    style={{ borderLeftColor: b.color }}
                  >
                    <div className="flex flex-[0_0_56px] flex-col tabular-nums">
                      <span className="font-semibold">{PERIODS[b.from][1]}</span>
                      <span className="whitespace-nowrap text-xs text-ink-3">{periodText(b)}</span>
                    </div>
                    <div className="flex min-w-0 flex-1 flex-col gap-0.5">
                      <span className="truncate font-semibold">{b.name}</span>
                      <span className="truncate font-mono text-xs text-ink-2">
                        {[b.code, b.room].filter(Boolean).join(" · ")}
                      </span>
                      {b.change && <Tag change={b.change} />}
                    </div>
                  </li>
                ))}
              </ol>
            ) : (
              <div className="rounded-[10px] bg-row-hover p-5 text-center text-[13px] text-ink-3">
                No classes on {DAYS[day - 1]}
              </div>
            )}
          </div>
        </>
      )}
    </section>
  );
}
