// The Sync page's two dialogs: suspending the scheduler and retrying jobs.

import { useState } from "react";
import { Users } from "lucide-react";
import { Button } from "@/components/ui/button";
import { FormDialog } from "@/components/page";
import { cn } from "@/lib/cn";
import { Segmented } from "./controls";
import { clock } from "./format";
import { QUEUE, QueueBar, type QueueKey } from "./SyncCharts";
import type { MoodleStatus } from "./types";

const HOURS = ["1", "2", "4", "8", "12", "24"] as const;

export function SuspendDialog({
  jobs,
  pending,
  onClose,
  onConfirm,
}: {
  jobs: number;
  pending: boolean;
  onClose: () => void;
  onConfirm: (hours: number) => void;
}) {
  const [hours, setHours] = useState<(typeof HOURS)[number]>("4");
  const h = Number(hours);
  const now = new Date();
  const resume = new Date(now.getTime() + h * 3_600_000);
  const nextDay = resume.toDateString() !== now.toDateString();
  return (
    <FormDialog
      open
      onOpenChange={(open) => !open && onClose()}
      title="Suspend Moodle sync"
      meta={`${jobs.toLocaleString("en-US")} jobs stay queued`}
      width={520}
      footer={
        <>
          <Button variant="line" size="form" onClick={onClose}>
            Cancel
          </Button>
          <Button variant="ink" size="form" disabled={pending} onClick={() => onConfirm(h)}>
            Suspend for {h} h
          </Button>
        </>
      }
    >
      <div className="flex flex-col gap-2">
        <span id="dur-label" className="text-[13px] font-medium">
          Duration
        </span>
        <Segmented
          radio
          label="Duration"
          value={hours}
          onChange={setHours}
          options={HOURS.map((n) => ({ value: n, label: `${n} h` }))}
          className="rounded-[10px]"
          itemClassName="min-h-9 flex-1 rounded-lg tabular-nums"
        />
      </div>
      <div className="flex flex-col gap-2 pb-1">
        <div role="img" aria-label={`Resumes at ${clock(resume)}`} className="relative h-3.5 rounded-full bg-divider">
          <div className="absolute inset-y-0 left-0 rounded-full bg-dot-warn" style={{ width: `${(h / 24) * 100}%` }} />
        </div>
        <div aria-hidden="true" className="flex justify-between text-xs">
          <div className="flex flex-col">
            <span className="text-ink-3">Now</span>
            <span className="font-semibold tabular-nums">{clock(now)}</span>
          </div>
          <div className="flex flex-col items-end">
            <span className="inline-flex items-center gap-1.5 text-ink-3">
              <span className="size-2 rounded-full bg-dot-warn" />
              Resumes
            </span>
            <span className="font-semibold tabular-nums">
              {clock(resume)}
              {nextDay && " +1d"}
            </span>
          </div>
        </div>
      </div>
    </FormDialog>
  );
}

type Scope = "failed" | "all";

export function RetryDialog({
  counts,
  pending,
  onClose,
  onConfirm,
}: {
  counts: MoodleStatus["job_counts"];
  pending: boolean;
  onClose: () => void;
  onConfirm: (scope: Scope, notify: boolean) => void;
}) {
  const [scope, setScope] = useState<Scope>("failed");
  const [notify, setNotify] = useState(false);
  const color = Object.fromEntries(QUEUE.map((q) => [q.key, q.color])) as Record<QueueKey, string>;
  // The routes' own picks (portal/app/routes/moodle/jobs.py); neither touches a running job.
  const scopes: Record<Scope, { label: string; count: number; taken: QueueKey[]; parts: [string, number, string][] }> = {
    failed: {
      label: "Failed jobs",
      count: counts.failed + counts.disabled + counts.pending_with_error,
      taken: ["failed", "disabled"],
      parts: [
        ["Failed", counts.failed, color.failed],
        ["Disabled", counts.disabled, color.disabled],
        ["Pending with an error", counts.pending_with_error, "var(--color-dot-off)"],
      ],
    },
    all: {
      label: "All jobs",
      count: counts.active + counts.failed + counts.disabled,
      taken: ["active", "failed", "disabled"],
      parts: [
        ["Active", counts.active, color.active],
        ["Failed", counts.failed, color.failed],
        ["Disabled", counts.disabled, color.disabled],
      ],
    },
  };
  const users = counts.waiting_to_sign_in;
  return (
    <FormDialog
      open
      onOpenChange={(open) => !open && onClose()}
      title="Retry sync jobs"
      meta="Runs now · attempts and errors reset"
      width={560}
      footer={
        <>
          <Button variant="line" size="form" onClick={onClose}>
            Cancel
          </Button>
          <Button
            variant="ink"
            size="form"
            disabled={pending || scopes[scope].count === 0}
            onClick={() => onConfirm(scope, notify)}
          >
            Retry {scopes[scope].count.toLocaleString("en-US")} jobs
          </Button>
        </>
      }
    >
      <div role="radiogroup" aria-label="Jobs" className="flex flex-col gap-2">
        {(Object.keys(scopes) as Scope[]).map((id) => {
          const s = scopes[id];
          const on = id === scope;
          return (
            <button
              key={id}
              type="button"
              role="radio"
              aria-checked={on}
              onClick={() => setScope(id)}
              className={cn(
                "flex items-start gap-3 rounded-xl border-2 bg-surface px-4 py-3.5 text-left",
                on ? "border-ink" : "border-line hover:border-field",
              )}
            >
              <span
                aria-hidden="true"
                className={cn(
                  "mt-px flex size-[18px] shrink-0 items-center justify-center rounded-full border-2",
                  on ? "border-ink" : "border-dot-off",
                )}
              >
                {on && <span className="size-2 rounded-full bg-ink" />}
              </span>
              <span className="flex min-w-0 flex-1 flex-col gap-2">
                <span className="flex items-baseline justify-between gap-3">
                  <span className="font-semibold">{s.label}</span>
                  <span className="text-[13px] tabular-nums text-ink-2">{s.count.toLocaleString("en-US")} jobs</span>
                </span>
                <QueueBar counts={counts} taken={s.taken} className="h-2" />
                <span className="flex flex-wrap gap-x-3 gap-y-1 text-xs text-ink-2">
                  {s.parts.map(([label, n, swatch]) => (
                    <span key={label} className="inline-flex items-center gap-[5px]">
                      <span aria-hidden="true" className="size-2 rounded-sm" style={{ background: swatch }} />
                      {label} {n.toLocaleString("en-US")}
                    </span>
                  ))}
                </span>
              </span>
            </button>
          );
        })}
      </div>
      <label className="flex min-h-11 cursor-pointer items-center gap-2.5 rounded-xl bg-ground px-3.5 py-2">
        <input
          type="checkbox"
          checked={notify}
          onChange={(e) => setNotify(e.target.checked)}
          className="size-[18px] accent-ink"
        />
        <span className="flex-1">Send the expire notice to users waiting to sign in</span>
        <span className="inline-flex items-center gap-1 whitespace-nowrap rounded-full bg-soft px-2 py-0.5 text-xs font-medium text-ink-2">
          <Users className="h-3 w-3" />
          {users} {users === 1 ? "user" : "users"}
        </span>
      </label>
    </FormDialog>
  );
}
