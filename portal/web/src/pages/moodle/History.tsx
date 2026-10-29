// One student's sync jobs, where each stands in its interval, and their runs.

import { DataTable, useTable, type Column } from "@/components/data-table";
import { DurationBar, JOB_TYPES, StatusPill, ago, dayClock, duration, jobType, statusLabel, until } from "./format";
import type { SyncJob, SyncRun } from "./types";

/** How far the job is from its last success to its next run. */
function progress(j: SyncJob): number {
  if (!j.last_success_at || !j.run_after) return 0;
  const from = Date.parse(j.last_success_at);
  const to = Date.parse(j.run_after);
  return to > from ? Math.min(100, Math.max(0, ((Date.now() - from) / (to - from)) * 100)) : 100;
}

export function JobCards({ jobs }: { jobs: SyncJob[] }) {
  return (
    <section
      aria-labelledby="jobs-h"
      className="box-border flex min-w-0 max-w-[360px] flex-[1_1_320px] flex-col gap-3 rounded-[14px] border border-line bg-surface px-5 pb-5 pt-4 tablet:max-w-full tablet:flex-[1_1_100%]"
    >
      <h2 id="jobs-h" className="text-[15px] font-semibold">
        Sync jobs
      </h2>
      {jobs.length === 0 && <div className="py-4 text-center font-semibold">Nothing here</div>}
      {jobs.map((j) => (
        <div key={j.id} className="flex flex-col gap-2.5 rounded-xl border border-line px-3.5 py-3">
          <div className="flex min-w-0 items-center gap-2">
            <span className="min-w-0 truncate font-semibold">{jobType(j.job_type)}</span>
            <span className="ml-auto shrink-0">
              <StatusPill status={j.job_status} dot={false} />
            </span>
          </div>
          <div className="flex flex-col gap-1">
            <div aria-hidden="true" className="h-1.5 rounded-full bg-divider">
              <div className="h-1.5 rounded-full bg-chart-1" style={{ width: `${progress(j)}%` }} />
            </div>
            <div className="flex flex-wrap justify-between gap-x-3 gap-y-0.5 text-xs tabular-nums text-ink-2">
              {j.last_success_at && <span className="whitespace-nowrap">Succeeded {ago(j.last_success_at)}</span>}
              {j.run_after && j.job_status !== "disabled" && (
                <span className="whitespace-nowrap">Next {until(j.run_after)}</span>
              )}
            </div>
          </div>
          <div className="flex flex-wrap gap-x-3.5 gap-y-1 text-xs text-ink-3">
            <span className="whitespace-nowrap">
              {j.attempts} {j.attempts === 1 ? "attempt" : "attempts"}
            </span>
            {j.last_failure_at && (
              <span className="whitespace-nowrap">Last failure {dayClock(j.last_failure_at).replace("Yesterday", "yesterday")}</span>
            )}
          </div>
          {j.last_error && <span className="break-words text-xs text-badge-danger-fg">{j.last_error}</span>}
        </div>
      ))}
    </section>
  );
}

const COLUMNS: Column<SyncRun>[] = [
  {
    key: "started",
    label: "Started",
    sortValue: (r) => Date.parse(r.started_at),
    className: "whitespace-nowrap text-[13px] tabular-nums",
    cell: (r) => dayClock(r.started_at),
  },
  {
    key: "type",
    label: "Type",
    sortValue: (r) => jobType(r.job_type),
    filterValue: (r) => jobType(r.job_type),
    filterOptions: JOB_TYPES,
    className: "text-[13px]",
    cell: (r) => jobType(r.job_type),
  },
  {
    key: "status",
    label: "Status",
    sortValue: (r) => statusLabel(r.status),
    filterValue: (r) => statusLabel(r.status),
    cell: (r) => <StatusPill status={r.status} />,
  },
  {
    key: "duration",
    label: "Duration",
    align: "right",
    hide: "phone",
    sortValue: (r) => duration(r.started_at, r.finished_at) ?? -1,
    cell: (r) => <DurationBar seconds={duration(r.started_at, r.finished_at)} barWidth={52} />,
  },
  {
    key: "fc",
    label: "Fetched · changed",
    align: "right",
    hide: "phone",
    className: "whitespace-nowrap font-mono text-[13px]",
    cell: (r) => (r.status === "succeeded" ? `${r.fetched_count ?? 0} · ${r.changed_count ?? 0}` : "—"),
  },
  {
    key: "error",
    label: "Error",
    hide: "tablet",
    className: "text-[13px]",
    cell: (r) =>
      r.error ? (
        <div title={r.error} className="max-w-[150px] truncate text-badge-danger-fg">
          {r.error}
        </div>
      ) : (
        <span className="text-ink-3">—</span>
      ),
  },
];

export function RunHistory({ runs }: { runs: SyncRun[] }) {
  const table = useTable({ rows: runs, columns: COLUMNS, perPage: 7, initialSort: { key: "started", dir: "desc" } });
  return (
    <section aria-labelledby="hist-h" className="min-w-0 flex-[999_1_560px] rounded-[14px] border border-line bg-surface">
      <div className="flex items-baseline justify-between border-b border-line px-6 py-4 phone:px-4">
        <h2 id="hist-h" className="text-[15px] font-semibold">
          Run history
        </h2>
        <span className="text-xs text-ink-3">{runs.length} runs</span>
      </div>
      <DataTable table={table} rowKey={(r) => String(r.id)} minWidth={640} empty="Nothing here" />
    </section>
  );
}
