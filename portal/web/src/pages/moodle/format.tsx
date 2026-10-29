// Words, times and status pills the two Moodle pages share.

import { api, ApiError } from "@/lib/api";
import { cn } from "@/lib/cn";
import { monthDayTime } from "@/lib/format";
import { platformLabel } from "@/lib/platform";
import { Pill, type Tone } from "@/components/page";

const JOB_TYPE: Record<string, string> = {
  moodle_assignments: "Moodle assignments",
  ntust_courses: "NTUST courses",
};
export const JOB_TYPES = Object.values(JOB_TYPE);
export const jobType = (t: string) => JOB_TYPE[t] ?? t;

function span(mins: number): string {
  if (mins < 60) return `${mins}m`;
  if (mins < 1440) return `${Math.floor(mins / 60)}h ${mins % 60}m`;
  return `${Math.floor(mins / 1440)}d ${Math.floor((mins % 1440) / 60)}h`;
}

/** "12s ago", "21m ago", "3h 4m ago" */
export function ago(iso: string | null | undefined): string {
  if (!iso) return "—";
  const secs = Math.max(0, Math.floor((Date.now() - Date.parse(iso)) / 1000));
  return secs < 60 ? `${secs}s ago` : `${span(Math.floor(secs / 60))} ago`;
}

/** "in 7h 39m", or "due 5m ago" once passed. */
export function until(iso: string): string {
  const diff = Date.parse(iso) - Date.now();
  const text = span(Math.floor(Math.abs(diff) / 60_000));
  return diff < 0 ? `due ${text} ago` : `in ${text}`;
}

const pad = (n: number) => String(n).padStart(2, "0");

/** "08:31" */
export function clock(iso: string | Date, seconds = false): string {
  const d = new Date(iso);
  return `${pad(d.getHours())}:${pad(d.getMinutes())}${seconds ? `:${pad(d.getSeconds())}` : ""}`;
}

/** "08:31", "Yesterday 08:31" or "Oct 27 08:31" */
export function dayClock(iso: string): string {
  const d = new Date(iso);
  const days = Math.round(
    (new Date().setHours(0, 0, 0, 0) - new Date(iso).setHours(0, 0, 0, 0)) / 86_400_000,
  );
  if (days === 0) return clock(d);
  if (days === 1) return `Yesterday ${clock(d)}`;
  return monthDayTime(iso);
}

/** "1151" -> "115-1" */
export const termLabel = (code: string) => `${code.slice(0, 3)}-${code.slice(3)}`;

const DEVICE: Record<string, string> = { ios: "iPhone", ipados: "iPad", macos: "Mac" };
export const deviceNoun = (platform: string) => DEVICE[platform] ?? platformLabel(platform);

/** The reason a portal route gave: our routes answer `error`, FastAPI `detail`. */
export function errorText(e: unknown): string {
  if (e instanceof ApiError && e.body && typeof e.body === "object") {
    const body = e.body as { error?: unknown; detail?: unknown };
    const reason = body.error ?? body.detail;
    if (reason) return typeof reason === "string" ? reason : JSON.stringify(reason);
  }
  return e instanceof Error ? e.message : String(e);
}

/** A POST whose `ok: false` answer counts as a failure too. */
export async function post<T = Record<string, unknown>>(path: string, json?: unknown): Promise<T> {
  const body = await api<T & { ok?: boolean; error?: string }>(path, { method: "POST", json });
  if (body && body.ok === false) throw new Error(body.error ?? "Failed");
  return body;
}

const STATUS: Record<string, [string, Tone, string]> = {
  pending: ["Pending", "neutral", "bg-dot-off"],
  running: ["Running", "info", "bg-link"],
  processing: ["Processing", "warning", "bg-dot-warn"],
  failed: ["Failed", "danger", "bg-dot-bad"],
  disabled: ["Disabled", "warning", "bg-dot-warn"],
  succeeded: ["Succeeded", "success", "bg-dot-ok"],
  sent: ["Sent", "success", "bg-dot-ok"],
  skipped: ["Skipped", "neutral", "bg-dot-off"],
  cancelled: ["Cancelled", "neutral", "bg-dot-off"],
  partial_failed: ["Failed", "danger", "bg-dot-bad"],
};

export const statusLabel = (s: string) => STATUS[s]?.[0] ?? s;

export function StatusPill({ status, dot = true }: { status: string; dot?: boolean }) {
  const [label, tone, color] = STATUS[status] ?? [status, "neutral" as Tone, "bg-dot-off"];
  return (
    <Pill tone={tone}>
      {dot && <span aria-hidden="true" className={cn("size-[7px] rounded-full", color)} />}
      {label}
    </Pill>
  );
}

const LA_SCENARIO: Record<string, string> = {
  classPreparing: "class preparing",
  inClass: "in class",
  assignmentUrgent: "assignment due",
};

/** What a push job is, in the words the apps use. */
export function pushLabel(j: { channel: string; scenario: string; kind: string | null }): string {
  const offset = /^reminder_(\d+(?:\.\d+)?)(m|h)$/.exec(j.scenario);
  const lead = offset ? ` · ${offset[1]} ${offset[2] === "m" ? "min" : "h"} before` : "";
  switch (j.channel) {
    case "schedule":
      return j.kind === "live_activity_end"
        ? "Live Activity end"
        : `Live Activity start · ${LA_SCENARIO[j.scenario] ?? j.scenario}`;
    case "course":
      return `Class reminder${lead}`;
    case "assignment":
      return `Assignment due reminder${lead}`;
    case "bulletin":
      return "Bulletin";
    case "custom":
      return "Custom push";
    case "system":
      if (j.scenario === "sync_trigger") return "Silent sync trigger";
      if (j.scenario === "reauth_required") return "Sign-in expired notice";
      return `System · ${j.scenario}`;
    default:
      return `${j.channel} · ${j.scenario}`;
  }
}

/** The run's length in seconds, or null while it runs. */
export function duration(started: string | null, finished: string | null): number | null {
  return started && finished ? (Date.parse(finished) - Date.parse(started)) / 1000 : null;
}

/** "3.2 s" with a bar against 12 s, the scale the design draws. */
export function DurationBar({ seconds }: { seconds: number | null }) {
  return (
    <div className="flex items-center justify-end gap-2">
      <span className="font-mono text-[13px]">{seconds === null ? "—" : `${seconds.toFixed(1)} s`}</span>
      <span aria-hidden="true" className="block h-1.5 w-[60px] rounded-full bg-divider">
        <span
          className="block h-1.5 rounded-full bg-chart-1"
          style={{ width: `${Math.min(100, ((seconds ?? 0) / 12) * 100)}%` }}
        />
      </span>
    </div>
  );
}
