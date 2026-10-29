// The Sync page's lists: every sync job, and the last 50 runs.

import { useState } from "react";
import { Link } from "react-router-dom";
import { Search, SearchX } from "lucide-react";
import { DataTable, PiiToggle, useTable, type Column } from "@/components/data-table";
import { TabBar } from "./controls";
import {
  DurationBar,
  JOB_TYPES,
  StatusPill,
  ago,
  clock,
  duration,
  jobType,
  statusLabel,
  until,
} from "./format";
import type { GlobalSyncJob, SyncRunRow } from "./types";

const inspect = (studentId: string) => (
  <Link
    to={`/inspect?student=${encodeURIComponent(studentId)}`}
    className="font-mono text-[13px] text-link hover:text-link-hover"
  >
    {studentId}
  </Link>
);

const student = <R extends { student_id: string }>(): Column<R> => ({
  key: "student",
  label: "Student",
  pii: 120,
  sortValue: (r) => r.student_id,
  cell: (r) => inspect(r.student_id),
});

const type = <R extends { job_type: string }>(hide?: "phone"): Column<R> => ({
  key: "type",
  label: "Type",
  hide,
  sortValue: (r) => jobType(r.job_type),
  filterValue: (r) => jobType(r.job_type),
  filterOptions: JOB_TYPES,
  className: "whitespace-nowrap text-[13px]",
  cell: (r) => jobType(r.job_type),
});

function nextRun(j: GlobalSyncJob): [string, number] {
  if (j.status === "disabled" || !j.run_after) return ["—", Infinity];
  if (j.status === "running") return ["now", -Infinity];
  return [until(j.run_after), Date.parse(j.run_after)];
}

const JOB_COLUMNS: Column<GlobalSyncJob>[] = [
  student(),
  type(),
  {
    key: "status",
    label: "Status",
    sortValue: (j) => statusLabel(j.status),
    filterValue: (j) => statusLabel(j.status),
    filterOptions: ["Pending", "Running", "Failed", "Disabled"],
    cell: (j) => <StatusPill status={j.status} />,
  },
  {
    key: "next",
    label: "Next run",
    hide: "phone",
    sortValue: (j) => nextRun(j)[1],
    className: "whitespace-nowrap text-[13px] tabular-nums text-ink-2",
    cell: (j) => nextRun(j)[0],
  },
  {
    key: "last",
    label: "Last success",
    hide: "tablet",
    sortValue: (j) => (j.last_success_at ? -Date.parse(j.last_success_at) : Infinity),
    className: "whitespace-nowrap text-[13px] tabular-nums text-ink-2",
    cell: (j) => ago(j.last_success_at),
  },
  {
    key: "error",
    label: "Error",
    hide: "tablet",
    className: "text-[13px]",
    cell: (j) =>
      j.last_error ? (
        <div title={j.last_error} className="max-w-[220px] truncate text-badge-danger-fg">
          {j.last_error}
        </div>
      ) : (
        <span className="text-ink-3">—</span>
      ),
  },
  {
    key: "attempts",
    label: "Attempts",
    align: "right",
    hide: "phone",
    sortValue: (j) => j.attempts,
    className: "font-mono text-[13px]",
    cell: (j) => j.attempts,
  },
];

const count = (n: number | null, r: SyncRunRow) => (r.status === "succeeded" && n !== null ? n : null);
const numberCell = (n: number | null) => (n === null ? <span className="text-ink-3">—</span> : n);

const RUN_COLUMNS: Column<SyncRunRow>[] = [
  student(),
  type("phone"),
  {
    key: "status",
    label: "Status",
    sortValue: (r) => statusLabel(r.status),
    filterValue: (r) => statusLabel(r.status),
    filterOptions: ["Running", "Succeeded", "Failed"],
    cell: (r) => <StatusPill status={r.status} />,
  },
  {
    key: "started",
    label: "Started",
    sortValue: (r) => (r.started_at ? Date.parse(r.started_at) : 0),
    className: "whitespace-nowrap text-[13px] tabular-nums",
    cell: (r) => (r.started_at ? clock(r.started_at) : "—"),
  },
  {
    key: "duration",
    label: "Duration",
    align: "right",
    hide: "phone",
    sortValue: (r) => duration(r.started_at, r.finished_at) ?? -1,
    cell: (r) => <DurationBar seconds={duration(r.started_at, r.finished_at)} />,
  },
  {
    key: "fetched",
    label: "Fetched",
    align: "right",
    hide: "tablet",
    sortValue: (r) => count(r.fetched_count, r) ?? -1,
    className: "font-mono text-[13px]",
    cell: (r) => numberCell(count(r.fetched_count, r)),
  },
  {
    key: "changed",
    label: "Changed",
    align: "right",
    hide: "tablet",
    sortValue: (r) => count(r.changed_count, r) ?? -1,
    className: "font-mono text-[13px]",
    cell: (r) => numberCell(count(r.changed_count, r)),
  },
];

const matchStudent = (r: { student_id: string }, q: string) => r.student_id.toUpperCase().includes(q);

export function SyncLists({ jobs, runs }: { jobs: GlobalSyncJob[]; runs: SyncRunRow[] }) {
  const [tab, setTab] = useState<"jobs" | "runs">("jobs");
  const [query, setQuery] = useState("");
  const [showPii, setShowPii] = useState(false);
  const search = query.trim().toUpperCase();
  const jobTable = useTable({
    rows: jobs,
    columns: JOB_COLUMNS,
    initialSort: { key: "next", dir: "asc" },
    search,
    match: matchStudent,
  });
  const runTable = useTable({
    rows: runs,
    columns: RUN_COLUMNS,
    initialSort: { key: "started", dir: "desc" },
    search,
    match: matchStudent,
  });

  const empty = (clearFilters: () => void, title: string) => (
    <div className="flex flex-col items-center gap-2.5 py-4">
      <div aria-hidden="true" className="flex size-11 items-center justify-center rounded-full bg-ground">
        <SearchX className="h-5 w-5" />
      </div>
      <span className="text-[15px] font-semibold text-ink">{title}</span>
      <button
        type="button"
        onClick={() => {
          setQuery("");
          clearFilters();
        }}
        className="min-h-9 px-2 text-[13px] text-link hover:text-link-hover"
      >
        Clear search and filters
      </button>
    </div>
  );

  return (
    <section aria-label="Jobs and runs" className="min-w-0 rounded-[14px] border border-line bg-surface">
      <div className="flex flex-wrap items-end justify-between gap-x-3 border-b border-line px-6 pt-3 phone:px-4">
        <TabBar
          label="Lists"
          value={tab}
          onChange={setTab}
          tabs={[
            { value: "jobs", label: "Jobs", count: jobs.length },
            { value: "runs", label: "Recent runs", count: runs.length },
          ]}
        />
        <div className="mb-2.5 flex items-center gap-2">
          <label className="flex h-9 w-[240px] max-w-full items-center gap-2 rounded-[9px] border border-field px-3 text-ink-3">
            <Search className="h-[15px] w-[15px] shrink-0" />
            <input
              type="search"
              aria-label="Student ID"
              placeholder="Student ID"
              value={query}
              onChange={(e) => setQuery(e.target.value)}
              className="min-w-0 flex-1 bg-transparent font-mono text-[13px] text-ink outline-none placeholder:font-sans placeholder:text-ink-3"
            />
          </label>
          <PiiToggle shown={showPii} onToggle={() => setShowPii((s) => !s)} />
        </div>
      </div>
      {tab === "jobs" ? (
        <DataTable
          table={jobTable}
          rowKey={(j) => String(j.id)}
          minWidth={900}
          showPii={showPii}
          empty={empty(jobTable.clearFilters, "No matching jobs")}
        />
      ) : (
        <DataTable
          table={runTable}
          rowKey={(r) => String(r.id)}
          minWidth={820}
          showPii={showPii}
          empty={empty(runTable.clearFilters, "No matching runs")}
        />
      )}
    </section>
  );
}
