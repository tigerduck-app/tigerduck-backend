// Terms, holidays, dates and NTUST periods for the Academic pages. Dates stay
// "YYYY-MM-DD" strings and day arithmetic runs in UTC, so no time zone moves a day.

import { api } from "@/lib/api";

export type Term = {
  code: string;
  start_date: string;
  end_date: string;
  name_zh: string;
  name_en: string;
};

export type Holiday = {
  id: number;
  name_zh: string;
  name_en: string;
  start_date: string;
  end_date: string;
  /** Users who chose reminders on this holiday anyway. */
  overrides: number;
};

export type Calendar = {
  /** Newest code first. */
  terms: Term[];
  holidays: Holiday[];
  /** Codes clients have filed courses under that have no dates yet. */
  undated_terms: string[];
};

export const CALENDAR_KEY = ["academic-calendar"];
export const fetchCalendar = () => api<Calendar>("/api/academic-calendar");

/** "1151" -> "115-1", how both apps print a term. */
export function termLabel(code: string): string {
  return code.length === 4 ? `${code.slice(0, 3)}-${code.slice(3)}` : code;
}

const DAY_MS = 86_400_000;
const MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];
/** Indexed by ISO weekday, 1 (Monday) to 7 (Sunday). */
export const WEEKDAYS = ["", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"];

export const toMs = (iso: string) => Date.parse(`${iso}T00:00:00Z`);
export const toIso = (ms: number) => new Date(ms).toISOString().slice(0, 10);
export const addDays = (iso: string, n: number) => toIso(toMs(iso) + n * DAY_MS);
export const daysBetween = (a: string, b: string) => Math.round((toMs(b) - toMs(a)) / DAY_MS);
export const isoWeekday = (iso: string) => new Date(toMs(iso)).getUTCDay() || 7;
export const mondayOf = (iso: string) => addDays(iso, 1 - isoWeekday(iso));

/** Today in Taipei, where the school keeps its calendar. */
export const todayIso = () => new Date().toLocaleDateString("en-CA", { timeZone: "Asia/Taipei" });

/** Every day from `from` to `to`, both included. */
export function eachDay(from: string, to: string): string[] {
  const out: string[] = [];
  for (let d = from; d <= to; d = addDays(d, 1)) out.push(d);
  return out;
}

/** "Nov 5" */
export function shortDate(iso: string): string {
  return `${MONTHS[Number(iso.slice(5, 7)) - 1]} ${Number(iso.slice(8, 10))}`;
}

/** "Nov 5, 2026" */
export const longDate = (iso: string) => `${shortDate(iso)}, ${iso.slice(0, 4)}`;

/** "Thu, Nov 5" */
export const dayDate = (iso: string) => `${WEEKDAYS[isoWeekday(iso)]}, ${shortDate(iso)}`;

/** "Sep 14, 2026 – Jan 8, 2027", or "Sep 14 – Dec 3, 2026" within one year. */
export function rangeText(from: string, to: string): string {
  if (from === to) return longDate(from);
  return from.slice(0, 4) === to.slice(0, 4)
    ? `${shortDate(from)} – ${longDate(to)}`
    : `${longDate(from)} – ${longDate(to)}`;
}

export const weeks = (from: string, to: string) => Math.ceil((daysBetween(from, to) + 1) / 7);

/** Each holiday day mapped to "國慶日 National Day". */
export function holidayNames(holidays: readonly Holiday[]): Map<string, string> {
  const out = new Map<string, string>();
  for (const h of holidays) {
    for (const d of eachDay(h.start_date, h.end_date)) out.set(d, `${h.name_zh} ${h.name_en}`);
  }
  return out;
}

// NTUST's periods, as the app's PeriodTimes in tigerduck-app/TigerDuck
// swift/TigerDuck/App/AppConstants.swift; the backend's course_period_start_times has the starts.
export const PERIOD_TIMES: Record<string, readonly [string, string]> = {
  "1": ["08:10", "09:00"],
  "2": ["09:10", "10:00"],
  "3": ["10:20", "11:10"],
  "4": ["11:20", "12:10"],
  "5": ["12:20", "13:10"],
  "6": ["13:20", "14:10"],
  "7": ["14:20", "15:10"],
  "8": ["15:30", "16:20"],
  "9": ["16:30", "17:20"],
  "10": ["17:30", "18:20"],
  A: ["18:25", "19:15"],
  B: ["19:20", "20:10"],
  C: ["20:15", "21:05"],
  D: ["21:10", "22:00"],
};
const PERIOD_ORDER = Object.keys(PERIOD_TIMES);

/** Consecutive periods grouped: ["2", "3", "6"] -> [["2", "3"], ["6"]]. */
export function periodRuns(periods: readonly string[]): string[][] {
  const runs: string[][] = [];
  for (const p of periods) {
    const last = runs.at(-1);
    const i = PERIOD_ORDER.indexOf(p);
    if (last && i > 0 && PERIOD_ORDER[i - 1] === last.at(-1)) last.push(p);
    else runs.push([p]);
  }
  return runs;
}

/** "Periods 3–4", "Periods 2, 6", "Period 2" */
export function periodsText(periods: readonly string[]): string {
  const runs = periodRuns(periods).map((r) => (r.length > 1 ? `${r[0]}–${r.at(-1)}` : r[0]));
  return `${periods.length === 1 ? "Period" : "Periods"} ${runs.join(", ")}`;
}

/** The first period's start and the last one's end; null for a period the table lacks. */
export function periodSpan(periods: readonly string[]): [string, string] | null {
  const first = PERIOD_TIMES[periods[0]];
  const last = PERIOD_TIMES[periods.at(-1) ?? ""];
  return first && last ? [first[0], last[1]] : null;
}
