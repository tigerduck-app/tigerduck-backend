import { useState, type ReactNode } from "react";
import { ArrowRight, CalendarDays, ChevronLeft, ChevronRight, Trash2, Users } from "lucide-react";
import { Button } from "@/components/ui/button";
import { ErrorAlert, FormDialog, fieldClass } from "@/components/page";
import { cn } from "@/lib/cn";
import {
  dayDate,
  isoWeekday,
  PERIOD_TIMES,
  periodRuns,
  periodSpan,
  periodsText,
  shortDate,
  toIso,
  WEEKDAYS,
  type Term,
} from "@/lib/academic";
import { classDays, KIND, KINDS, meetingOn, roomOf, type Change, type Course, type Kind } from "./model";

export type ChangeBody = {
  course_no: string;
  kind: Kind;
  mode: "day" | "range";
  day?: string;
  from?: string;
  to?: string;
  excluded_weekdays?: number[];
  periods?: string[];
  room: string;
  start: string;
  end: string;
  note_zh: string;
  note_en: string;
};

const MONTH_NAMES = [
  "January", "February", "March", "April", "May", "June",
  "July", "August", "September", "October", "November", "December",
];
const NOTE_PRESETS: [string, string][] = [
  ["期中考", "Midterm exam"],
  ["期末考", "Final exam"],
  ["小考", "Quiz"],
  ["補課", "Make-up class"],
];
const HHMM = /^([01][0-9]|2[0-3]):[0-5][0-9]$/;
const mins = (hhmm: string) => Number(hhmm.slice(0, 2)) * 60 + Number(hhmm.slice(3, 5));

/** Adds a change to `course`, or edits `change`. */
export function ChangeDialog({
  course,
  term,
  holidays,
  today,
  existing,
  change,
  initDay,
  pending,
  error,
  onClose,
  onSave,
  onDelete,
}: {
  course: Course;
  term: Term;
  holidays: ReadonlyMap<string, string>;
  today: string;
  /** The course's other changes, marked in the calendar. */
  existing: readonly Change[];
  change: Change | null;
  initDay: string | null;
  pending: boolean;
  error: string | null;
  onClose: () => void;
  onSave: (body: ChangeBody) => void;
  onDelete: () => void;
}) {
  const [mode, setMode] = useState(change?.mode ?? "day");
  const [kind, setKind] = useState<Kind | null>(change?.kind ?? null);
  const [day, setDay] = useState(change?.day ?? initDay);
  const [from, setFrom] = useState(change?.from ?? null);
  const [to, setTo] = useState(change?.to ?? null);
  const [off, setOff] = useState<number[]>(change?.excluded_weekdays ?? []);
  // null: every period of the day.
  const [chosen, setChosen] = useState<string[] | null>(change?.periods ?? null);
  const [room, setRoom] = useState(change?.room ?? "");
  const [start, setStart] = useState(change?.start ?? "");
  const [end, setEnd] = useState(change?.end ?? "");
  const [noteZh, setNoteZh] = useState(change?.note_zh ?? "");
  const [noteEn, setNoteEn] = useState(change?.note_en ?? "");
  const firstMonth = term.start_date.slice(0, 7);
  const lastMonth = term.end_date.slice(0, 7);
  const [month, setMonth] = useState(() => {
    const anchor = ((mode === "range" ? from : day) ?? today).slice(0, 7);
    return anchor < firstMonth ? firstMonth : anchor > lastMonth ? lastMonth : anchor;
  });

  const changeOn = new Map<string, Kind>();
  for (const c of existing) for (const d of c.class_days) changeOn.set(d, c.kind);
  const isClassDay = (iso: string) =>
    iso >= term.start_date && iso <= term.end_date && !holidays.has(iso) && meetingOn(course, iso) !== undefined;

  const isRange = mode === "range";
  const hasDates = isRange ? Boolean(from && to) : Boolean(day);
  const days = isRange ? (from && to ? classDays(course, term, holidays, from, to, off) : []) : day ? [day] : [];
  const meeting = days[0] ? meetingOn(course, days[0]) : undefined;
  const dayPeriods = !isRange && meeting ? meeting.periods : [];
  const showChips = !isRange && meeting !== undefined && (periodRuns(dayPeriods).length > 1 || chosen !== null);
  const periods = showChips && chosen ? chosen : (meeting?.periods ?? []);
  const span = periodSpan(periods);
  const regStart = span?.[0] ?? "";
  const regEnd = span?.[1] ?? "";
  const regRoom = meeting ? roomOf(course, meeting.day, periods) : "";
  const startNew = kind === "start" ? start || regStart : regStart;
  const endNew = kind === "end" ? end || regEnd : regEnd;

  const pickDay = (iso: string) => {
    if (!isRange) {
      if (iso !== day) setChosen(null);
      setDay(iso);
    } else if (!from || to || iso < from) {
      setFrom(iso);
      setTo(null);
    } else {
      setTo(iso);
    }
  };
  const togglePeriod = (p: string) => {
    const current = chosen ?? dayPeriods;
    const next = current.includes(p) ? current.filter((x) => x !== p) : dayPeriods.filter((x) => x === p || current.includes(x));
    setChosen(next.length === dayPeriods.length ? null : next);
  };
  const shift = (n: number) => {
    const [y, m] = month.split("-").map(Number);
    setMonth(toIso(Date.UTC(y, m - 1 + n, 1)).slice(0, 7));
  };

  const valid =
    kind !== null &&
    hasDates &&
    days.length > 0 &&
    periods.length > 0 &&
    (kind !== "room" || room.trim() !== "") &&
    (kind !== "note" || noteZh.trim() !== "") &&
    (kind !== "start" || HHMM.test(startNew)) &&
    (kind !== "end" || HHMM.test(endNew));

  const save = () => {
    if (!valid) return;
    onSave({
      course_no: course.course_no,
      kind,
      mode,
      ...(isRange ? { from: from!, to: to!, excluded_weekdays: off } : { day: day! }),
      // No periods means the whole day.
      ...(showChips && chosen ? { periods: chosen } : {}),
      room: room.trim(),
      start: startNew,
      end: endNew,
      note_zh: noteZh.trim(),
      note_en: noteEn.trim(),
    });
  };

  const editing = change !== null;
  return (
    <FormDialog
      open
      onOpenChange={(open) => !open && onClose()}
      width={880}
      title={editing ? "Edit change" : "Add change"}
      meta={
        <>
          <span className="font-mono">{course.course_no}</span> · {course.name}
          {course.name_en ? ` · ${course.name_en}` : ""}
        </>
      }
      footer={
        <>
          {editing && (
            <Button
              variant="line"
              size="form"
              className="gap-1.5 border-danger/30 px-3.5 font-normal text-danger hover:text-danger"
              onClick={onDelete}
            >
              <Trash2 className="!size-[15px]" />
              Delete
            </Button>
          )}
          <span className="mr-auto flex items-center gap-1.5 pl-1 text-[13px] text-ink-3 max-[720px]:hidden">
            {hasDates && (
              <>
                <Users className="h-[15px] w-[15px]" />
                {course.students} users · {course.devices} devices
              </>
            )}
          </span>
          <Button variant="line" size="form" onClick={onClose}>
            Cancel
          </Button>
          <Button variant="ink" size="form" disabled={!valid || pending} onClick={save}>
            {editing ? "Save" : isRange && days.length > 1 ? `Add to ${days.length} meetings` : "Add change"}
          </Button>
        </>
      }
    >
      {error && <ErrorAlert className="items-center">{error}</ErrorAlert>}
      <div className="flex flex-wrap gap-6">
        <div className="flex min-w-0 flex-[0_1_312px] flex-col gap-3 max-[720px]:basis-full">
          <Segmented
            label="Dates"
            options={[["day", "Single day"], ["range", "Range"]]}
            value={mode}
            onChange={(m) => {
              if (m === "day") {
                if (!day) setChosen(null);
                setDay(day ?? from);
              } else {
                setFrom(from ?? day);
              }
              setMode(m);
            }}
          />
          <div className="flex items-center justify-between">
            <Button variant="quiet" size="icon" aria-label="Previous month" className="rounded-[9px] text-ink-2" disabled={month <= firstMonth} onClick={() => shift(-1)}>
              <ChevronLeft />
            </Button>
            <span className="font-semibold">
              {MONTH_NAMES[Number(month.slice(5, 7)) - 1]} {month.slice(0, 4)}
            </span>
            <Button variant="quiet" size="icon" aria-label="Next month" className="rounded-[9px] text-ink-2" disabled={month >= lastMonth} onClick={() => shift(1)}>
              <ChevronRight />
            </Button>
          </div>
          <MonthGrid
            month={month}
            today={today}
            holidays={holidays}
            changeOn={changeOn}
            isClassDay={isClassDay}
            selected={(iso) => (isRange ? iso === from || iso === to : iso === day)}
            between={(iso) => isRange && from !== null && to !== null && iso > from && iso < to}
            covered={(iso) => days.includes(iso)}
            onPick={pickDay}
          />
          <div className="flex flex-wrap gap-x-3.5 gap-y-1.5 text-xs text-ink-2">
            <Swatch className="size-1.5 rounded-full bg-chart-1">Class day</Swatch>
            <Swatch className="size-3 rounded bg-badge-warning ring-1 ring-inset ring-badge-warning-fg/30">Holiday</Swatch>
            <Swatch className="h-[3px] w-3.5 rounded-sm bg-badge-violet-fg">Has a change</Swatch>
            <Swatch className="size-3 rounded ring-[1.5px] ring-inset ring-link">Today</Swatch>
          </div>
        </div>

        <div className="flex min-w-0 flex-[1_1_400px] flex-col gap-[18px]">
          <div role="radiogroup" aria-label="Change" className="grid grid-cols-5 gap-2 max-[720px]:grid-cols-3">
            {KINDS.map((k) => {
              const on = kind === k.id;
              const Icon = k.icon;
              return (
                <button
                  key={k.id}
                  type="button"
                  role="radio"
                  aria-checked={on}
                  onClick={() => setKind(k.id)}
                  className={cn(
                    "flex min-h-[72px] min-w-0 flex-col items-center justify-center gap-1.5 rounded-xl border-2 px-1 py-2 text-[13px] font-medium text-ink",
                    on ? k.card : "border-line bg-surface hover:border-field",
                  )}
                >
                  <Icon className={cn("h-5 w-5", k.text)} />
                  <span>{k.short}</span>
                </button>
              );
            })}
          </div>

          <div className="flex flex-col gap-2">
            <span className="text-xs font-medium text-ink-3">Applies to</span>
            {!hasDates ? (
              <div className="flex min-h-11 items-center gap-2 rounded-[10px] border-[1.5px] border-dashed border-field px-3.5 text-[13px] text-ink-3">
                <CalendarDays className="h-4 w-4" />
                {isRange ? (from ? "Pick the last day" : "Pick the first day") : "Pick a class day"}
              </div>
            ) : (
              <>
                <div className="flex flex-wrap items-center gap-2">
                  <span className="inline-flex min-h-8 items-center rounded-full bg-ink px-3 text-[13px] font-medium text-on-ink">
                    {isRange ? `${shortDate(from!)} – ${shortDate(to!)}` : dayDate(day!)}
                  </span>
                  <span className="text-[13px] text-ink-2">
                    {isRange
                      ? `${days.length} ${days.length === 1 ? "meeting" : "meetings"}`
                      : [periods.length ? periodsText(periods) : "", span?.join("–"), regRoom].filter(Boolean).join(" · ")}
                  </span>
                  {isRange && (
                    <div role="group" aria-label="Weekdays" className="ml-auto flex gap-1.5">
                      {course.meetings.map((m) => (
                        <Chip
                          key={m.day}
                          on={!off.includes(m.day)}
                          onClick={() => setOff(off.includes(m.day) ? off.filter((d) => d !== m.day) : [...off, m.day])}
                        >
                          {WEEKDAYS[m.day]}
                        </Chip>
                      ))}
                    </div>
                  )}
                </div>
                {showChips && (
                  <div role="group" aria-label="Periods" className="flex flex-wrap gap-1.5">
                    {dayPeriods.map((p) => (
                      <Chip key={p} on={periods.includes(p)} onClick={() => togglePeriod(p)}>
                        {p}
                        {PERIOD_TIMES[p] && <span className="ml-1.5 tabular-nums opacity-75">{PERIOD_TIMES[p][0]}</span>}
                      </Chip>
                    ))}
                  </div>
                )}
              </>
            )}
          </div>

          {kind === "room" && (
            <label className="flex w-[220px] max-w-full flex-col gap-1.5 text-[13px] font-medium">
              New room
              <input
                value={room}
                onChange={(e) => setRoom(e.target.value)}
                placeholder="IB-302"
                maxLength={128}
                className={cn(fieldClass, "px-3 font-mono font-normal")}
              />
            </label>
          )}
          {(kind === "start" || kind === "end") && (
            <div className="flex flex-col gap-3">
              <label className="flex w-[180px] flex-col gap-1.5 text-[13px] font-medium">
                {kind === "end" ? "New end" : "New start"}
                <input
                  type="time"
                  value={kind === "end" ? endNew : startNew}
                  onChange={(e) => (kind === "end" ? setEnd : setStart)(e.target.value)}
                  className={cn(fieldClass, "px-3 font-normal")}
                />
              </label>
              {regStart && <TimeAxis regular={[regStart, regEnd]} changed={[startNew, endNew]} />}
            </div>
          )}
          {kind === "note" && (
            <div className="flex flex-col gap-2.5">
              <div role="group" aria-label="Common notes" className="flex flex-wrap gap-1.5">
                {NOTE_PRESETS.map(([zh, en]) => (
                  <button
                    key={zh}
                    type="button"
                    onClick={() => {
                      setNoteZh(zh);
                      setNoteEn(en);
                    }}
                    className="min-h-8 rounded-full border border-field bg-surface px-3 text-[13px] hover:bg-row-hover"
                  >
                    {zh} {en}
                  </button>
                ))}
              </div>
              <div className="flex flex-wrap gap-3">
                <label className="flex flex-[1_1_180px] flex-col gap-1.5 text-[13px] font-medium">
                  Chinese
                  <input value={noteZh} onChange={(e) => setNoteZh(e.target.value)} className={cn(fieldClass, "px-3 font-normal")} />
                </label>
                <label className="flex flex-[1_1_180px] flex-col gap-1.5 text-[13px] font-medium">
                  <span>
                    English <span className="font-normal text-ink-3">optional</span>
                  </span>
                  <input
                    value={noteEn}
                    onChange={(e) => setNoteEn(e.target.value)}
                    placeholder={noteZh}
                    className={cn(fieldClass, "px-3 font-normal")}
                  />
                </label>
              </div>
            </div>
          )}

          {kind && hasDates && meeting && (
            <Preview
              kind={kind}
              name={course.name}
              start={[regStart, startNew]}
              end={[regEnd, endNew]}
              room={[regRoom, room.trim()]}
              note={noteZh.trim() ? (noteEn.trim() ? `${noteZh.trim()} · ${noteEn.trim()}` : noteZh.trim()) : ""}
            />
          )}
        </div>
      </div>
    </FormDialog>
  );
}

function Segmented<T extends string>({
  label,
  options,
  value,
  onChange,
}: {
  label: string;
  options: [T, string][];
  value: T;
  onChange: (v: T) => void;
}) {
  return (
    <div role="radiogroup" aria-label={label} className="flex gap-1 rounded-[10px] bg-soft p-1">
      {options.map(([v, text]) => (
        <button
          key={v}
          type="button"
          role="radio"
          aria-checked={value === v}
          onClick={() => onChange(v)}
          className={cn(
            "min-h-[34px] flex-1 rounded-[7px] text-[13px]",
            value === v ? "bg-surface font-semibold text-ink shadow-[0_1px_2px_rgba(20,22,26,0.12)]" : "text-ink-2",
          )}
        >
          {text}
        </button>
      ))}
    </div>
  );
}

function Chip({ on, onClick, children }: { on: boolean; onClick: () => void; children: ReactNode }) {
  return (
    <button
      type="button"
      aria-pressed={on}
      onClick={onClick}
      className={cn(
        "inline-flex min-h-[30px] items-center rounded-full border px-2.5 text-xs",
        on ? "border-ink bg-ink text-on-ink" : "border-field bg-surface text-ink-2 hover:bg-row-hover",
      )}
    >
      {children}
    </button>
  );
}

function Swatch({ className, children }: { className: string; children: string }) {
  return (
    <span className="flex items-center gap-1.5">
      <span aria-hidden="true" className={className} />
      {children}
    </span>
  );
}

function MonthGrid({
  month,
  today,
  holidays,
  changeOn,
  isClassDay,
  selected,
  between,
  covered,
  onPick,
}: {
  month: string;
  today: string;
  holidays: ReadonlyMap<string, string>;
  changeOn: ReadonlyMap<string, Kind>;
  isClassDay: (iso: string) => boolean;
  selected: (iso: string) => boolean;
  between: (iso: string) => boolean;
  covered: (iso: string) => boolean;
  onPick: (iso: string) => void;
}) {
  const [y, m] = month.split("-").map(Number);
  const first = `${month}-01`;
  const lead = isoWeekday(first) - 1;
  const count = new Date(Date.UTC(y, m, 0)).getUTCDate();
  return (
    <div className="grid grid-cols-7 gap-1">
      {WEEKDAYS.slice(1).map((w) => (
        <span key={w} className="text-center text-[11px] font-medium text-ink-3">
          {w}
        </span>
      ))}
      {Array.from({ length: lead }, (_, i) => (
        <div key={`lead-${i}`} />
      ))}
      {Array.from({ length: count }, (_, i) => {
        const iso = toIso(Date.UTC(y, m - 1, i + 1));
        const isClass = isClassDay(iso);
        const past = iso < today;
        const holiday = holidays.get(iso);
        const on = selected(iso);
        const kind = changeOn.get(iso);
        const title = [dayDate(iso), holiday, isClass ? "class" : null, kind ? "has a change" : null]
          .filter(Boolean)
          .join(" · ");
        return (
          <div key={iso} className="flex justify-center">
            <button
              type="button"
              title={title}
              aria-label={title}
              aria-pressed={on}
              // Past days stay readable but cannot be picked.
              disabled={!isClass || past}
              onClick={() => onPick(iso)}
              className={cn(
                "flex size-10 flex-col items-center justify-center gap-0.5 rounded-[10px] text-[13px] tabular-nums disabled:cursor-default",
                on
                  ? "bg-ink font-semibold text-on-ink"
                  : between(iso)
                    ? covered(iso) ? "bg-badge-info font-semibold text-badge-info-fg" : "bg-ground text-dot-off"
                    : holiday
                      ? "bg-badge-warning text-badge-warning-fg"
                      : isClass && !past
                        ? "bg-surface font-medium text-ink hover:bg-soft"
                        : "text-dot-off",
                iso === today && !on && "ring-[1.5px] ring-inset ring-link",
              )}
            >
              <span>{i + 1}</span>
              <span
                aria-hidden="true"
                className={cn(
                  kind && isClass ? "h-[3px] w-3.5 rounded-sm" : "size-[5px] rounded-full",
                  !isClass
                    ? "bg-transparent"
                    : on
                      ? "bg-on-ink"
                      : kind
                        ? KIND[kind].fill
                        : past ? "bg-chart-1/40" : "bg-chart-1",
                )}
              />
            </button>
          </div>
        );
      })}
    </div>
  );
}

function TimeAxis({ regular, changed }: { regular: [string, string]; changed: [string, string] }) {
  const a0 = 8 * 60;
  // 08:00 to 18:30, or on to the hour after the latest time for an evening class.
  const a1 = Math.max(18 * 60 + 30, Math.ceil(Math.max(...[...regular, ...changed].map(mins)) / 60) * 60);
  const pos = (a: string, b: string) => ({
    left: `${((mins(a) - a0) / (a1 - a0)) * 100}%`,
    width: `${(Math.max(0, mins(b) - mins(a)) / (a1 - a0)) * 100}%`,
  });
  const ticks: string[] = [];
  for (let h = 8; h * 60 <= a1; h += 2) ticks.push(`${String(h).padStart(2, "0")}:00`);
  return (
    <div aria-hidden="true" className="relative h-[46px]">
      <div className="absolute inset-x-0 top-2.5 h-[18px] rounded-md bg-ground" />
      <div style={pos(...regular)} className="absolute top-2.5 h-[18px] rounded-md bg-field" />
      <div style={pos(...changed)} className="absolute top-1.5 h-[26px] rounded-[7px] border-2 border-badge-orange-fg bg-badge-orange/70" />
      {ticks.map((t) => (
        <span
          key={t}
          style={{ left: `${((mins(t) - a0) / (a1 - a0)) * 100}%` }}
          className="absolute bottom-0 -translate-x-1/2 text-[11px] tabular-nums text-ink-3"
        >
          {t}
        </span>
      ))}
    </div>
  );
}

function Preview({
  kind,
  name,
  start,
  end,
  room,
  note,
}: {
  kind: Kind;
  name: string;
  start: [string, string];
  end: [string, string];
  room: [string, string];
  note: string;
}) {
  const cancelled = kind === "cancelled";
  const startChanged = kind === "start" && start[1] !== start[0];
  const endChanged = kind === "end" && end[1] !== end[0];
  const roomChanged = kind === "room" && room[1] !== "";
  const struck = "text-dot-off line-through";
  return (
    <div className="flex flex-col gap-2">
      <span className="text-xs font-medium text-ink-3">In the apps</span>
      <div className={cn("flex items-stretch gap-3 rounded-xl border border-line px-3.5 py-3", cancelled ? "bg-ground" : "bg-surface")}>
        <div className="flex min-w-[52px] flex-col items-end tabular-nums">
          <span className={startChanged || cancelled ? cn("text-[13px]", struck) : "font-semibold"}>{start[0]}</span>
          {startChanged && <span className="font-semibold text-badge-orange-fg">{start[1]}</span>}
          <span className={cn("text-xs", endChanged || cancelled ? struck : "text-ink-3")}>{end[0]}</span>
          {endChanged && <span className="text-xs font-semibold text-badge-orange-fg">{end[1]}</span>}
        </div>
        <div aria-hidden="true" className={cn("w-1 rounded-full", cancelled ? "bg-field" : "bg-chart-1")} />
        <div className="flex min-w-0 flex-1 flex-col gap-1.5">
          <span className={cn("font-semibold", cancelled ? struck : "text-ink")}>{name}</span>
          <div className="flex flex-wrap items-center gap-1.5">
            {room[0] && (
              <span
                className={cn(
                  "rounded-md bg-ground px-2 py-0.5 font-mono text-xs font-medium",
                  roomChanged || cancelled ? struck : "text-ink-2",
                )}
              >
                {room[0]}
              </span>
            )}
            {roomChanged && (
              <>
                <ArrowRight className="h-3.5 w-3.5 text-ink-3" />
                <span className="rounded-md bg-badge-info px-2 py-0.5 font-mono text-xs font-medium text-badge-info-fg">{room[1]}</span>
              </>
            )}
            {kind === "note" && note && (
              <span className="rounded-full bg-badge-violet px-2 py-0.5 text-xs font-semibold text-badge-violet-fg">{note}</span>
            )}
            {cancelled && (
              <span className="rounded-full bg-badge-danger px-2 py-0.5 text-xs font-semibold text-badge-danger-fg">Cancelled</span>
            )}
          </div>
        </div>
      </div>
    </div>
  );
}
