// Moodle › Sync: the server-side sync job queue, its runs, and the switches
// that pause or retry it. Data inspection lives in ./moodle/Inspection.

import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { toast } from "sonner";
import { Pause, Play, RefreshCw } from "lucide-react";
import { api } from "@/lib/api";
import { Button } from "@/components/ui/button";
import { Skeleton } from "@/components/ui/skeleton";
import { PageHeading } from "@/components/page";
import { LoadError } from "./moodle/controls";
import { clock, errorText, post } from "./moodle/format";
import { Kpis, QueueCard, RunsChart } from "./moodle/SyncCharts";
import { RetryDialog, SuspendDialog } from "./moodle/SyncDialogs";
import { SyncLists } from "./moodle/SyncLists";
import type { GlobalSyncJob, MoodleStatus, SyncStatsData } from "./moodle/types";

export { DataInspectionPage } from "./moodle/Inspection";

function every(seconds: number): string {
  return seconds % 3600 === 0 ? `${seconds / 3600} h` : `${Math.round(seconds / 60)} min`;
}

function untilLabel(iso: string): string {
  const at = new Date(iso);
  return `until ${clock(at)} ${at.toDateString() === new Date().toDateString() ? "today" : "tomorrow"}`;
}

export function MoodlePage() {
  const qc = useQueryClient();
  const [dialog, setDialog] = useState<"suspend" | "retry" | null>(null);

  const statusQ = useQuery({
    queryKey: ["moodle", "status"],
    queryFn: () => api<MoodleStatus>("/api/moodle/status"),
    refetchInterval: 10_000,
  });
  const statsQ = useQuery({
    queryKey: ["moodle", "stats"],
    queryFn: () => api<SyncStatsData>("/api/moodle/stats"),
    refetchInterval: 10_000,
  });
  const jobsQ = useQuery({
    queryKey: ["moodle", "jobs"],
    queryFn: () => api<{ jobs: GlobalSyncJob[] }>("/api/moodle/jobs?limit=5000"),
    refetchInterval: 30_000,
  });
  const refresh = () => qc.invalidateQueries({ queryKey: ["moodle"] });

  const suspendMut = useMutation({
    mutationFn: (hours: number) => post("/api/moodle/suspend", { hours }),
    onSuccess: (_, hours) => {
      toast.success(`Moodle sync suspended for ${hours} h`);
      setDialog(null);
      refresh();
    },
    onError: (e) => toast.error(errorText(e)),
  });
  const resumeMut = useMutation({
    mutationFn: () => post("/api/moodle/resume"),
    onSuccess: () => {
      toast.success("Moodle sync resumed");
      refresh();
    },
    onError: (e) => toast.error(errorText(e)),
  });
  const retryMut = useMutation({
    mutationFn: ({ scope, notify }: { scope: "failed" | "all"; notify: boolean }) =>
      post<{ retried: number; notified: number }>(
        scope === "all" ? "/api/moodle/retry-all-jobs" : "/api/moodle/retry-all",
        { notify },
      ),
    onSuccess: ({ retried, notified }) => {
      toast.success(`Re-queued ${retried} jobs${notified ? `, notified ${notified} users` : ""}`);
      setDialog(null);
      refresh();
    },
    onError: (e) => toast.error(errorText(e)),
  });

  const status = statusQ.data;
  const stats = statsQ.data;
  const failed = [statusQ, statsQ, jobsQ].find((q) => q.isError);
  const total = status
    ? status.job_counts.active + status.job_counts.running + status.job_counts.failed + status.job_counts.disabled
    : 0;

  return (
    <>
      <PageHeading
        group="Moodle"
        title="Sync"
        // server/config.py's sync_job_tick_seconds and sync_job_batch_size; the portal cannot read them.
        meta={status?.interval_seconds ? `Tick 30 s · batch 5 · every ${every(status.interval_seconds)} per student` : undefined}
        actions={
          <>
            <Button variant="line" size="form" disabled={!status} onClick={() => setDialog("retry")}>
              <RefreshCw />
              Retry jobs
            </Button>
            {status && !status.suspended_until && (
              <Button variant="ink" size="form" onClick={() => setDialog("suspend")}>
                <Pause />
                Suspend
              </Button>
            )}
          </>
        }
      />

      {failed && (
        <LoadError title="Couldn’t load sync status" detail={errorText(failed.error)} onRetry={refresh} />
      )}

      {status?.suspended_until && (
        <div
          role="status"
          className="flex flex-wrap items-center justify-between gap-3 rounded-xl border border-dot-warn/40 bg-badge-warning px-[18px] py-3.5 text-badge-warning-fg"
        >
          <div className="flex items-center gap-3">
            <Pause className="h-[18px] w-[18px]" />
            <span className="flex flex-wrap gap-x-1.5">
              <strong className="font-semibold">Suspended</strong>
              <span>{untilLabel(status.suspended_until)}</span>
            </span>
          </div>
          <button
            type="button"
            disabled={resumeMut.isPending}
            onClick={() => resumeMut.mutate()}
            className="flex min-h-9 items-center gap-1.5 rounded-[9px] border border-dot-warn/60 bg-surface px-3.5 disabled:opacity-60"
          >
            <Play className="h-[15px] w-[15px]" />
            Resume now
          </button>
        </div>
      )}

      {status ? <QueueCard status={status} /> : !failed && <Skeleton className="h-[118px] rounded-[14px]" />}
      {stats && <Kpis summary={stats.summary} />}
      {stats && <RunsChart hourly={stats.hourly} />}
      {!stats && !failed && <Skeleton className="h-[320px] rounded-[14px]" />}
      <SyncLists jobs={jobsQ.data?.jobs ?? []} runs={stats?.recent_runs ?? []} />

      {dialog === "suspend" && (
        <SuspendDialog
          jobs={total}
          pending={suspendMut.isPending}
          onClose={() => setDialog(null)}
          onConfirm={(hours) => suspendMut.mutate(hours)}
        />
      )}
      {dialog === "retry" && status && (
        <RetryDialog
          counts={status.job_counts}
          pending={retryMut.isPending}
          onClose={() => setDialog(null)}
          onConfirm={(scope, notify) => retryMut.mutate({ scope, notify })}
        />
      )}
    </>
  );
}
