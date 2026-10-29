import { cn } from "@/lib/cn";
import { addDays, dayDate, mondayOf, periodsText, WEEKDAYS, weeks, type Term } from "@/lib/academic";
import { Pill } from "@/components/page";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";
import { KIND, meetingOn, type Change, type Course } from "./model";

const MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];

const LEGEND: [string, string][] = [
  ["bg-chart-1/40", "Class"],
  ["bg-badge-warning ring-1 ring-inset ring-badge-warning-fg/30", "Holiday"],
  ["bg-badge-danger-fg", "Cancelled"],
  ["bg-badge-info-fg", "Room"],
  ["bg-badge-orange-fg", "Time"],
  ["bg-badge-violet-fg", "Note"],
  ["ring-2 ring-inset ring-ink", "Today"],
];

/** The term as weeks by weekday: class days, holidays and the course's changes. */
export function TermStrip({
  term,
  course,
  holidays,
  changes,
  today,
  onAdd,
  onEdit,
}: {
  term: Term;
  course: Course;
  holidays: ReadonlyMap<string, string>;
  changes: readonly Change[];
  today: string;
  onAdd: (day: string) => void;
  onEdit: (change: Change) => void;
}) {
  const monday = mondayOf(term.start_date);
  const weekCount = weeks(term.start_date, term.end_date);
  // Weekends only when the course meets then.
  const weekdays = [1, 2, 3, 4, 5, 6, 7].filter((d) => d <= 5 || course.meetings.some((m) => m.day === d));
  const changesOn = new Map<string, Change[]>();
  for (const c of changes) for (const d of c.class_days) changesOn.set(d, [...(changesOn.get(d) ?? []), c]);

  const months: { label: string; week: number }[] = [];
  for (let w = 0; w < weekCount; w++) {
    const label = MONTHS[Number(addDays(monday, w * 7).slice(5, 7)) - 1];
    if (months.at(-1)?.label !== label) months.push({ label, week: w });
  }

  return (
    <figure className="flex flex-col gap-2.5">
      <figcaption className="flex flex-wrap justify-between gap-x-3 gap-y-1 text-xs font-medium text-ink-3">
        <span>Term</span>
        <span className="phone:hidden">Click a class day to add a change</span>
      </figcaption>
      <div className="overflow-x-auto">
        <div
          style={{
            gridTemplateColumns: `32px repeat(${weekCount}, minmax(0, 1fr))`,
            gridTemplateRows: `16px 16px repeat(${weekdays.length}, 28px)`,
          }}
          className="grid min-w-[540px] gap-[3px]"
        >
          {months.map((m) => (
            <span
              key={`${m.label}-${m.week}`}
              style={{ gridColumn: `${m.week + 2} / span 3`, gridRow: 1 }}
              className="whitespace-nowrap text-[11px] font-semibold text-ink-2"
            >
              {m.label}
            </span>
          ))}
          {Array.from({ length: weekCount }, (_, w) => (
            <span key={`w${w}`} style={{ gridColumn: w + 2, gridRow: 2 }} className="text-center text-[10px] tabular-nums text-ink-3">
              {w + 1}
            </span>
          ))}
          {weekdays.map((d, r) => (
            <span key={`d${d}`} style={{ gridColumn: 1, gridRow: r + 3 }} className="text-[11px] leading-7 text-ink-3">
              {WEEKDAYS[d]}
            </span>
          ))}
          {Array.from({ length: weekCount }, (_, w) =>
            weekdays.map((d, r) => {
              const iso = addDays(monday, w * 7 + d - 1);
              const holiday = holidays.get(iso);
              const inTerm = iso >= term.start_date && iso <= term.end_date;
              const isClass = inTerm && !holiday && meetingOn(course, iso) !== undefined;
              const dayChanges = changesOn.get(iso) ?? [];
              // A note colors the day only when nothing else changes it.
              const change = dayChanges.find((c) => c.kind !== "note") ?? dayChanges[0];
              const past = iso < today;
              const actionable = !past && (isClass || change !== undefined);
              const pick = actionable && dayChanges.length > 1;
              const title = [
                dayDate(iso),
                ...(holiday ? [holiday] : []),
                ...(dayChanges.length ? dayChanges.map((c) => KIND[c.kind].label) : isClass ? ["class"] : []),
              ].join(" · ");
              const cell = (
                <button
                  key={iso}
                  type="button"
                  title={title}
                  aria-label={title}
                  disabled={!actionable}
                  onClick={pick ? undefined : () => (change ? onEdit(change) : onAdd(iso))}
                  style={{ gridColumn: w + 2, gridRow: r + 3 }}
                  className={cn(
                    "flex h-7 w-full items-center justify-center rounded-[5px] text-[10px] tabular-nums disabled:cursor-default",
                    change
                      ? cn(KIND[change.kind].fill, "font-semibold text-surface", past && "opacity-45")
                      : isClass
                        ? past ? "bg-chart-1/15 font-semibold text-ink-3" : "bg-chart-1/40 font-semibold text-ink"
                        : holiday
                          ? "bg-badge-warning text-badge-warning-fg ring-1 ring-inset ring-badge-warning-fg/30"
                          : "bg-ground text-ink-3",
                    iso === today && "ring-2 ring-inset ring-ink",
                  )}
                >
                  {Number(iso.slice(8, 10))}
                </button>
              );
              if (!pick) return cell;
              return (
                <DropdownMenu key={iso}>
                  <DropdownMenuTrigger asChild>{cell}</DropdownMenuTrigger>
                  <DropdownMenuContent align="start" className="rounded-xl border-line bg-surface p-1.5 text-ink">
                    {dayChanges.map((c) => (
                      <DropdownMenuItem key={c.id} onSelect={() => onEdit(c)} className="min-h-[34px] gap-2">
                        <Pill tone={KIND[c.kind].tone}>{KIND[c.kind].label}</Pill>
                        {c.periods && <span className="text-xs text-ink-3">{periodsText(c.periods)}</span>}
                      </DropdownMenuItem>
                    ))}
                  </DropdownMenuContent>
                </DropdownMenu>
              );
            }),
          )}
        </div>
      </div>
      <div className="flex flex-wrap gap-x-3.5 gap-y-1.5 text-xs text-ink-2">
        {LEGEND.map(([swatch, label]) => (
          <span key={label} className="flex items-center gap-1.5">
            <span aria-hidden="true" className={cn("size-3 rounded-[3px]", swatch)} />
            {label}
          </span>
        ))}
      </div>
    </figure>
  );
}
