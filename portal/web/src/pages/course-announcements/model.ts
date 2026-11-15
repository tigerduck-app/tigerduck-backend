// Course announcements as portal/app/routes/course_announcements.py serves
// them, and what the page derives from them: an item's change in the Course
// changes page's shape, its checks, each announcement's status and the
// evidence marked in its text.

import type { Tone } from "@/components/page";
import { WEEKDAYS, shortDate } from "@/lib/academic";
import { KIND, dateText, type Change, type Course, type Kind } from "../course-changes/model";

export const KEY = "course-announcements";

export type ItemState =
  | "pending" | "applied" | "approved" | "rejected" | "past" | "superseded"
  | "info" | "skipped" | "edited" | "removed";
export type Action = "add" | "replace" | "remove";
/** A check code to whether the item passed it. */
export type Checks = Record<string, boolean | { ok?: boolean; announcement_id?: string }>;

export type Item = {
  id: number;
  course_no: string;
  kind: "course_change" | "makeup" | "exam";
  action: Action | null;
  /** A change in `course_changes` column names, or an exam or make-up class. */
  payload: Record<string, unknown>;
  evidence: string | null;
  checks: Checks | null;
  passes_checks: boolean;
  /** The change a replace or remove acts on, an add clashes with, or an item wrote. */
  existing_change_id: number | null;
};

export type ReviewItem = Item & { announcement_id: string; subject: string; posted_at: string };

export type Review = {
  term: string;
  auto_apply: boolean;
  polled_at: string | null;
  decided: number;
  items: ReviewItem[];
};

export type Filter = "all" | "extracted" | "failed" | "differs";
export type ExtractionStatus = "queued" | "running" | "done" | "failed" | "superseded";

export type Listed = {
  id: string;
  course_codes: string[];
  subject: string;
  excerpt: string | null;
  posted_at: string;
  labels: { label: string; score: number | null }[];
  route: "unclassified" | "not_sent" | "sent";
  extraction: ExtractionStatus | null;
  extracted: boolean;
  differs: boolean;
  items: Record<"pending" | "applied" | "rejected" | "removed" | "past" | "exams", number>;
};

export type AnnouncementPage = {
  page: number;
  per_page: number;
  counts: Record<Filter, number>;
  announcements: Listed[];
};

export type DetailItem = Item & {
  state: ItemState;
  decided_by: string | null;
  decided_at: string | null;
  reason: string | null;
};

export type Detail = {
  id: string;
  term: string;
  course_codes: string[];
  subject: string;
  text: string | null;
  posted_at: string;
  modified_at: string;
  group_id: number;
  has_attachment: boolean;
  classifier_release: string | null;
  scores: Record<string, number> | null;
  labels: string[];
  route: Listed["route"];
  extraction: {
    status: ExtractionStatus;
    model: string | null;
    attempts: number;
    error_code: string | null;
    updated_at: string;
  } | null;
  /** The model of the newest finished extraction, whose items these are. */
  model: string | null;
  items: DetailItem[];
  skipped_codes: string[];
  moodle_url: string;
};

/** The exam and class labels the route rule reads, as the classifier names them. */
export const CLASS_LABELS = [
  "exam_info", "exam_change", "exam_seating", "exam_review",
  "exam_prep", "grade_release", "class_cancel", "class_change",
];

/** A course change item's payload: `course_changes` column names. */
type ChangePayload = {
  kind: Kind;
  mode: "day" | "range";
  start_date: string;
  end_date: string;
  excluded_weekdays?: number[];
  periods?: string[] | null;
  room?: string | null;
  start_time?: string | null;
  end_time?: string | null;
  note_zh?: string | null;
  note_en?: string | null;
};

/** A course change item's change as the Course changes page holds one. */
export function asChange(item: Item, term: string): Change {
  const p = item.payload as ChangePayload;
  const day = p.mode === "day";
  return {
    id: item.existing_change_id ?? 0,
    term,
    course_no: item.course_no,
    kind: p.kind,
    mode: p.mode,
    day: day ? p.start_date : null,
    from: day ? null : p.start_date,
    to: day ? null : p.end_date,
    excluded_weekdays: p.excluded_weekdays ?? [],
    periods: p.periods ?? null,
    class_days: [],
    stale: false,
    room: p.room ?? null,
    start: p.start_time ?? null,
    end: p.end_time ?? null,
    note_zh: p.note_zh ?? null,
    note_en: p.note_en ?? null,
    created_at: "",
    created_by: "",
    updated_at: "",
    updated_by: "",
  };
}

export const ACTION_LABEL: Record<Action, string | null> = { add: null, replace: "Replace", remove: "Remove" };

// Each check's line in the detail, and its pill when it fails.
const CHECKS: [string, string, string][] = [
  ["evidence", "Evidence in text", "No evidence"],
  ["term", "Within term", "Outside term"],
  ["class_day", "Class day", "Not a class day"],
  ["weekday", "Weekday matches date", "Weekday differs"],
  ["clash", "No existing change", "Existing change"],
  ["labels", "Labels agree", "Labels disagree"],
  ["certain", "Marked certain", "Not certain"],
  ["visibility", "Whole course", "Group post"],
  ["contradicted", "Not contradicted", "Contradicted"],
];

export type Check = {
  code: string;
  label: string;
  failed: string;
  ok: boolean;
  /** The announcement that contradicts the item. */
  by: string | null;
};

const passed = (v: Checks[string]) => v === true || (typeof v === "object" && v !== null && v.ok === true);

/** An item's checks in a fixed order, codes this page does not know last. */
export function checksOf(checks: Checks | null): Check[] {
  if (!checks) return [];
  const known = new Map(CHECKS.map(([code, label, failed]) => [code, { label, failed }]));
  const codes = [
    ...CHECKS.map(([code]) => code).filter((c) => c in checks),
    ...Object.keys(checks).filter((c) => !known.has(c)).sort(),
  ];
  return codes.map((code) => {
    const v = checks[code];
    return {
      code,
      label: known.get(code)?.label ?? code,
      failed: known.get(code)?.failed ?? code,
      ok: passed(v),
      by: typeof v === "object" && v !== null && typeof v.announcement_id === "string" ? v.announcement_id : null,
    };
  });
}

/** One line per check across the items: it passes when every item passes it. */
export function mergedChecks(items: readonly Item[]): (Check & { item: Item })[] {
  const out = new Map<string, Check & { item: Item }>();
  for (const item of items) {
    for (const c of checksOf(item.checks)) {
      const seen = out.get(c.code);
      if (!seen || (seen.ok && !c.ok)) out.set(c.code, { ...c, item });
    }
  }
  return [...out.values()];
}

const LONG_WEEKDAY = (iso: string) =>
  new Date(`${iso}T00:00:00Z`).toLocaleDateString("en-US", { weekday: "long", timeZone: "UTC" });

/** What a failed check found, where the page has it: "Meets Tue, Thu", "Dec 3 is a Thursday". */
export function checkNote(
  check: Check,
  change: Change | null,
  course: Course | undefined,
  existing: Change | undefined,
  labels: readonly string[],
): string {
  if (check.ok || !change) return "";
  const day = change.day ?? change.from ?? "";
  switch (check.code) {
    case "class_day":
      return course ? `Meets ${course.meetings.map((m) => WEEKDAYS[m.day]).join(", ")}` : "";
    case "weekday":
      return day ? `${shortDate(day)} is a ${LONG_WEEKDAY(day)}` : "";
    case "labels":
      return [labels.join(", "), KIND[change.kind]?.label.toLowerCase()].filter(Boolean).join(" · ");
    case "clash":
      return existing ? `${KIND[existing.kind].label} · ${dateText(existing)}` : "";
    default:
      return "";
  }
}

/** The list's status pill for an announcement. */
export function statusOf(a: Listed): [string, Tone] {
  if (a.route === "unclassified") return ["Not classified", "neutral"];
  if (a.extraction === "failed") return ["Failed", "danger"];
  if (!a.extracted) return a.extraction ? ["Queued", "neutral"] : ["Not sent", "neutral"];
  const n = a.items;
  if (n.pending) return [`${n.pending} pending`, "warning"];
  if (n.applied) return ["Applied", "success"];
  if (n.rejected) return ["Rejected", "neutral"];
  if (n.removed) return ["Removed", "neutral"];
  if (n.exams) return ["Exam", "info"];
  if (n.past) return ["Past", "neutral"];
  return ["No changes", "neutral"];
}

export const STATE_PILL: Partial<Record<ItemState, [string, Tone]>> = {
  applied: ["Applied", "success"],
  approved: ["Approved", "success"],
  rejected: ["Rejected", "neutral"],
  past: ["Past", "neutral"],
  superseded: ["Superseded", "neutral"],
  edited: ["Edited", "neutral"],
  removed: ["Removed", "neutral"],
};

export const EXAM_KIND: Record<string, string> = {
  midterm: "Midterm",
  final: "Final",
  quiz: "Quiz",
  other: "Exam",
};

/** `text` cut into runs, marking every place a span occurs. */
export function markRuns(text: string, spans: readonly string[]): { text: string; mark: boolean }[] {
  const marked = new Array<boolean>(text.length).fill(false);
  for (const span of spans) {
    if (!span) continue;
    for (let at = text.indexOf(span); at >= 0; at = text.indexOf(span, at + span.length)) {
      marked.fill(true, at, at + span.length);
    }
  }
  const runs: { text: string; mark: boolean }[] = [];
  for (let i = 0; i < text.length; i++) {
    const last = runs.at(-1);
    if (last && last.mark === marked[i]) last.text += text[i];
    else runs.push({ text: text[i], mark: marked[i] });
  }
  return runs;
}
