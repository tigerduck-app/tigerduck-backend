// Course changes as portal/app/routes/course_changes.py serves them, and what
// the page derives from them: class days, the regular time and room a change
// replaces, and the conflict copy for the API's 409 answers.

import { CalendarX, Clock, FileText, Hourglass, MapPin, type LucideIcon } from "lucide-react";
import type { Tone } from "@/components/page";
import { ApiError } from "@/lib/api";
import {
  dayDate,
  eachDay,
  isoWeekday,
  periodSpan,
  periodsText,
  shortDate,
  WEEKDAYS,
  type Term,
} from "@/lib/academic";
import { asMessage } from "../lists/format";

/** One weekday's periods; `day` is the ISO weekday. */
export type Meeting = { day: number; periods: string[] };

export type Course = {
  course_no: string;
  name: string;
  name_en: string | null;
  instructors: string[];
  credits: number | null;
  classroom: string | null;
  /** "<weekday>-<period>" to a room. */
  classroom_map: Record<string, string>;
  meetings: Meeting[];
  students: number;
  devices: number;
  changes: number;
};

export type Kind = "cancelled" | "room" | "start" | "end" | "note";

export type Change = {
  id: number;
  term: string;
  course_no: string;
  kind: Kind;
  mode: "day" | "range";
  day: string | null;
  from: string | null;
  to: string | null;
  excluded_weekdays: number[];
  /** null for every period of the day. */
  periods: string[] | null;
  /** The days the change still covers after calendar edits. */
  class_days: string[];
  /** No class day left: the change can only be deleted. */
  stale: boolean;
  room: string | null;
  start: string | null;
  end: string | null;
  note_zh: string | null;
  note_en: string | null;
  created_at: string;
  created_by: string;
  updated_at: string;
  updated_by: string;
};

export type KindInfo = {
  id: Kind;
  label: string;
  short: string;
  tone: Tone;
  icon: LucideIcon;
  /** The badge's text color as a fill, for calendar cells. */
  fill: string;
  text: string;
  soft: string;
  /** The picked kind's card. */
  card: string;
};

// Whole class names, so Tailwind finds them.
export const KINDS: KindInfo[] = [
  {
    id: "cancelled", label: "Cancelled", short: "Cancelled", tone: "danger", icon: CalendarX,
    fill: "bg-badge-danger-fg", text: "text-badge-danger-fg", soft: "bg-badge-danger",
    card: "border-badge-danger-fg bg-badge-danger",
  },
  {
    id: "room", label: "Room change", short: "Room", tone: "info", icon: MapPin,
    fill: "bg-badge-info-fg", text: "text-badge-info-fg", soft: "bg-badge-info",
    card: "border-badge-info-fg bg-badge-info",
  },
  {
    id: "start", label: "Start time", short: "Start time", tone: "orange", icon: Clock,
    fill: "bg-badge-orange-fg", text: "text-badge-orange-fg", soft: "bg-badge-orange",
    card: "border-badge-orange-fg bg-badge-orange",
  },
  {
    id: "end", label: "End time", short: "End time", tone: "orange", icon: Hourglass,
    fill: "bg-badge-orange-fg", text: "text-badge-orange-fg", soft: "bg-badge-orange",
    card: "border-badge-orange-fg bg-badge-orange",
  },
  {
    id: "note", label: "Note", short: "Note", tone: "violet", icon: FileText,
    fill: "bg-badge-violet-fg", text: "text-badge-violet-fg", soft: "bg-badge-violet",
    card: "border-badge-violet-fg bg-badge-violet",
  },
];
export const KIND = Object.fromEntries(KINDS.map((k) => [k.id, k])) as Record<Kind, KindInfo>;
export const KIND_LABELS = KINDS.map((k) => k.label);

export const firstDay = (c: Change) => (c.mode === "day" ? c.day! : c.from!);
export const lastDay = (c: Change) => (c.mode === "day" ? c.day! : c.to!);
/** The day a change turns past: its last class day, else its last date. */
export const finalDay = (c: Change) => c.class_days.at(-1) ?? lastDay(c);

export const meetingOn = (course: Course | undefined, iso: string) =>
  course?.meetings.find((m) => m.day === isoWeekday(iso));

/** The course's class days from `from` to `to`, as the backend's course_class_days counts them. */
export function classDays(
  course: Course,
  term: Term,
  holidays: ReadonlyMap<string, string>,
  from: string,
  to: string,
  excluded: readonly number[] = [],
): string[] {
  const lo = from < term.start_date ? term.start_date : from;
  const hi = to > term.end_date ? term.end_date : to;
  return eachDay(lo, hi).filter(
    (d) => !holidays.has(d) && !excluded.includes(isoWeekday(d)) && meetingOn(course, d) !== undefined,
  );
}

export function roomOf(course: Course, weekday: number, periods: readonly string[]): string {
  return course.classroom_map[`${weekday}-${periods[0]}`] ?? course.classroom ?? "";
}

export const reach = (c: Course) => `${c.students} users · ${c.devices} devices`;

export const noteText = (zh: string | null, en: string | null) => (en ? `${zh} · ${en}` : zh ?? "");

/** "Tue 3–4 · Thu 2–3 · TR-412" */
export function meetsText(course: Course): string {
  return [
    ...course.meetings.map((m) => `${WEEKDAYS[m.day]} ${periodsText(m.periods).replace(/^Periods? /, "")}`),
    ...(course.classroom ? [course.classroom] : []),
  ].join(" · ");
}

export type Detail = { old: string; next: string; note: string; periods: string };

/** What the change replaces on its first class day, and with what. */
export function detailOf(change: Change, course: Course | undefined): Detail {
  const meeting = meetingOn(course, change.class_days[0] ?? firstDay(change));
  const periods = change.periods ?? meeting?.periods ?? [];
  const span = periodSpan(periods);
  const room = course && meeting ? roomOf(course, meeting.day, periods) : "";
  const detail = { old: "", next: "", note: "", periods: periods.length ? periodsText(periods) : "" };
  switch (change.kind) {
    case "cancelled":
      return { ...detail, old: [span?.join("–"), room].filter(Boolean).join(" "), next: "No class" };
    case "room":
      return { ...detail, old: room, next: change.room ?? "" };
    case "start":
      return { ...detail, old: span?.[0] ?? "", next: change.start ?? "" };
    case "end":
      return { ...detail, old: span?.[1] ?? "", next: change.end ?? "" };
    case "note":
      return { ...detail, note: noteText(change.note_zh, change.note_en) };
  }
}

/** "Thu, Nov 5", or "Nov 16 – Jan 4" for a range. */
export const dateText = (c: Change) =>
  c.mode === "range" ? `${shortDate(c.from!)} – ${shortDate(c.to!)}` : dayDate(c.day!);

/** "Mon, Thu · 7 meetings" under a range. */
export function rangeSub(c: Change, course: Course | undefined): string {
  const days = (course?.meetings ?? [])
    .filter((m) => !c.excluded_weekdays.includes(m.day))
    .map((m) => WEEKDAYS[m.day]);
  const n = c.class_days.length;
  return [...(days.length ? [days.join(", ")] : []), `${n} ${n === 1 ? "meeting" : "meetings"}`].join(" · ");
}

/** The API's 409s as the dialog's conflict copy; other errors as the API words them. */
export function changeError(e: unknown, kind: Kind | null): string {
  if (e instanceof ApiError && e.status === 409) {
    const day = (e.body as { day?: unknown } | null)?.day;
    // The stale edit's detail ends "so the change can only be deleted".
    if (e.message.includes("can only be deleted")) return "No class day is left for this change. Delete it instead.";
    if (typeof day !== "string") return asMessage(e);
    if (kind === "cancelled") return `${dayDate(day)} is already cancelled.`;
    return `${dayDate(day)} already has a ${KIND[kind ?? "room"].label.toLowerCase()}.`;
  }
  return asMessage(e);
}
