// Moodle › Data inspection: one student's synced data, devices, queue and
// sync log. The student rides in the URL (?student=) so a link opens them.

import { useState } from "react";
import { useSearchParams } from "react-router-dom";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { toast } from "sonner";
import { Copy, RefreshCw, Search, SearchX, Trash2, Zap } from "lucide-react";
import { api } from "@/lib/api";
import { cn } from "@/lib/cn";
import { copyText } from "@/lib/copy";
import { Button } from "@/components/ui/button";
import { Skeleton } from "@/components/ui/skeleton";
import { Dot, EmptyState, PageHeading, Pill, type DotTone } from "@/components/page";
import { BackendPanel } from "./BackendPanel";
import { LoadError } from "./controls";
import { DevicePanel } from "./DevicePanel";
import { ago, clock, errorText, post } from "./format";
import { JobCards, RunHistory } from "./History";
import { ClearQueueDialog, LiveActivityDialog, ReauthDialog, type LiveActivityBody } from "./InspectDialogs";
import { LiveLog } from "./LiveLog";
import { StudentSearch } from "./StudentSearch";
import { Timetable } from "./Timetable";
import { Topology } from "./Topology";
import type { SyncCoursesResponse, SyncDevice, SyncEventsResponse } from "./types";

type Dialog = { kind: "clear" } | { kind: "reauth" } | { kind: "la"; device: SyncDevice } | null;

const card = "rounded-[14px] border border-line bg-surface";

export function DataInspectionPage() {
  const [params, setParams] = useSearchParams();
  const studentId = params.get("student") ?? "";
  const [query, setQuery] = useState(studentId);
  const pick = (id: string) => {
    setQuery(id);
    setParams({ student: id });
  };
  const stop = () => {
    setQuery("");
    setParams({});
  };

  return (
    <>
      <PageHeading group="Moodle" title="Data inspection" />
      {studentId ? (
        <Student key={studentId} studentId={studentId} query={query} setQuery={setQuery} onPick={pick} onStop={stop} />
      ) : (
        <>
          <section aria-label="Student" className={cn(card, "relative z-[5] px-6 py-4 phone:px-4")}>
            <StudentSearch value={query} onChange={setQuery} onPick={pick} />
          </section>
          <section className={card}>
            <EmptyState icon={Search} title="Search a student to start" />
          </section>
        </>
      )}
    </>
  );
}

function Student({
  studentId,
  query,
  setQuery,
  onPick,
  onStop,
}: {
  studentId: string;
  query: string;
  setQuery: (q: string) => void;
  onPick: (id: string) => void;
  onStop: () => void;
}) {
  const qc = useQueryClient();
  const [node, setNode] = useState("backend");
  const [dialog, setDialog] = useState<Dialog>(null);
  const sid = encodeURIComponent(studentId);

  const eventsQ = useQuery({
    queryKey: ["moodle", "events", studentId],
    queryFn: () => api<SyncEventsResponse>(`/api/moodle/sync-events?student_id=${sid}`),
    refetchInterval: 5_000,
  });
  const coursesQ = useQuery({
    queryKey: ["moodle", "courses", studentId],
    queryFn: () => api<SyncCoursesResponse>(`/api/moodle/sync-courses?student_id=${sid}`),
    refetchInterval: 10_000,
  });
  const refresh = () => qc.invalidateQueries({ queryKey: ["moodle", "events", studentId] });
  const done = (text: string, warn = false) => {
    (warn ? toast.warning : toast.success)(text);
    setDialog(null);
    void refresh();
  };
  const failed = (e: unknown) => toast.error(errorText(e));

  const syncPush = useMutation({
    mutationFn: () => post<{ deduplicated: boolean }>(`/api/moodle/push-sync-trigger?student_id=${sid}`),
    onSuccess: (r) =>
      r.deduplicated
        ? done("Deduplicated: a sync trigger already exists in this window", true)
        : done("Sync trigger queued and sent"),
    onError: failed,
  });
  const tick = useMutation({
    mutationFn: () => post<{ done?: boolean }>("/api/moodle/push-tick"),
    onSuccess: (r) =>
      r.done === false ? done("Push tick is still sending; the queue updates as it goes", true) : done("Push tick done"),
    onError: failed,
  });
  const clear = useMutation({
    mutationFn: () => post<{ cancelled: number }>(`/api/moodle/push-clear?student_id=${sid}`),
    onSuccess: (r) => done(`Cancelled ${r.cancelled} pending pushes`),
    onError: failed,
  });
  const reauth = useMutation({
    mutationFn: () => post<{ push_job_id: number; devices: number }>(`/api/moodle/tests/reauth-push?student_id=${sid}`),
    onSuccess: (r) => done(`Queued as job #${r.push_job_id} for ${r.devices} ${r.devices === 1 ? "device" : "devices"}`),
    onError: failed,
  });
  const liveActivity = useMutation({
    mutationFn: ({ device, body }: { device: SyncDevice; body: LiveActivityBody }) =>
      post<{ push_job_id: number; ends_at: string }>("/api/moodle/tests/live-activity", {
        student_id: studentId,
        device_id: device.id,
        ...body,
      }),
    onSuccess: (r) => done(`Started as job #${r.push_job_id}; it stays up until ${clock(r.ends_at)}`),
    onError: failed,
  });

  const data = eventsQ.data;
  const found = data?.found ? data : undefined;
  const devices = (found?.devices ?? []).filter((d) => !d.deleted_at);
  const name = found?.student?.display_name ?? null;
  const queued = found?.queued_jobs ?? [];
  const device = devices.find((d) => d.id === node);

  return (
    <>
      <section aria-label="Student" className={cn(card, "relative z-[5] flex flex-col gap-4 px-6 py-4 phone:px-4")}>
        <div className="flex flex-wrap items-center gap-x-4 gap-y-3">
          <StudentSearch value={query} onChange={setQuery} onPick={onPick} />
          {found && (
            <div className="ml-auto flex flex-wrap justify-end gap-2 phone:ml-0 phone:justify-start">
              <Button variant="line" size="bar" className="text-[13px] font-normal" disabled={syncPush.isPending} onClick={() => syncPush.mutate()}>
                <RefreshCw />
                Force sync push
              </Button>
              <Button variant="line" size="bar" className="text-[13px] font-normal" disabled={tick.isPending} onClick={() => tick.mutate()}>
                <Zap />
                Run push tick
              </Button>
              <Button
                variant="line"
                size="bar"
                className="border-badge-danger-fg/30 text-[13px] font-normal text-badge-danger-fg"
                disabled={queued.length === 0}
                onClick={() => setDialog({ kind: "clear" })}
              >
                <Trash2 />
                Clear queue
              </Button>
              <Button variant="quiet" size="bar" className="text-[13px] font-normal text-ink-2" onClick={onStop}>
                Stop
              </Button>
            </div>
          )}
        </div>
        {found && (
          <StudentCard
            studentId={studentId}
            name={name}
            credential={found.student?.credential_status ?? null}
            devices={devices}
            live={!eventsQ.isError}
            updatedAt={eventsQ.dataUpdatedAt}
          />
        )}
      </section>

      {eventsQ.isError && (
        <LoadError title="Couldn’t load this student" detail={errorText(eventsQ.error)} onRetry={() => void eventsQ.refetch()} />
      )}
      {!data && eventsQ.isPending && <Skeleton className="h-[420px] rounded-[14px]" />}
      {data && !data.found && (
        <section className={card}>
          <EmptyState icon={SearchX} title="No student matches" />
        </section>
      )}

      {found && (
        <div className="flex flex-col gap-6">
          <Timetable studentId={studentId} />
          <Topology data={found} devices={devices} selected={device ? node : "backend"} onSelect={setNode} />
          {device ? (
            <DevicePanel
              key={device.id}
              device={device}
              events={found}
              courses={coursesQ.data}
              studentId={studentId}
              onLiveActivity={() => setDialog({ kind: "la", device })}
            />
          ) : (
            <BackendPanel events={found} courses={coursesQ.data} devices={devices} onReauth={() => setDialog({ kind: "reauth" })} />
          )}
          <div className="flex flex-wrap items-start gap-6">
            <JobCards jobs={found.jobs ?? []} />
            <RunHistory runs={found.runs ?? []} />
          </div>
          <LiveLog studentId={studentId} />
        </div>
      )}

      {dialog?.kind === "clear" && (
        <ClearQueueDialog
          jobs={queued}
          studentId={studentId}
          name={name}
          pending={clear.isPending}
          onClose={() => setDialog(null)}
          onConfirm={() => clear.mutate()}
        />
      )}
      {dialog?.kind === "reauth" && (
        <ReauthDialog
          devices={devices}
          studentId={studentId}
          name={name}
          pending={reauth.isPending}
          onClose={() => setDialog(null)}
          onConfirm={() => reauth.mutate()}
        />
      )}
      {dialog?.kind === "la" && (
        <LiveActivityDialog
          device={dialog.device}
          pending={liveActivity.isPending}
          onClose={() => setDialog(null)}
          onConfirm={(body) => liveActivity.mutate({ device: dialog.device, body })}
        />
      )}
    </>
  );
}

function StudentCard({
  studentId,
  name,
  credential,
  devices,
  live,
  updatedAt,
}: {
  studentId: string;
  name: string | null;
  credential: string | null;
  devices: SyncDevice[];
  live: boolean;
  updatedAt: number;
}) {
  const seen = devices.map((d) => d.last_seen_at).filter((s): s is string => !!s).sort().at(-1) ?? null;
  const syncing = devices.some((d) => d.cloud_sync_enabled !== false);
  const facts: [string, string, DotTone?][] = [
    ["Devices", String(devices.length)],
    ["Last seen", ago(seen)],
    credential === null
      ? ["School account", "—"]
      : credential === "active"
        ? ["School account", "Signed in", "ok"]
        : ["School account", "Sign-in expired", "bad"],
    ["Cloud sync", syncing ? "On" : "Off", syncing ? "ok" : "off"],
  ];
  return (
    <div className="flex flex-wrap items-center gap-x-5 gap-y-3 border-t border-divider pt-3.5">
      <div className="flex min-w-0 items-center gap-3">
        <span aria-hidden="true" className="flex size-10 shrink-0 items-center justify-center rounded-full bg-soft text-base font-semibold">
          {(name ?? studentId).slice(0, 1)}
        </span>
        <div className="flex min-w-0 flex-col">
          {name && <span className="truncate text-[17px] font-semibold">{name}</span>}
          <span className="flex items-center gap-0.5">
            <span className={cn("font-mono", name ? "text-[13px] text-ink-2" : "text-[17px] font-semibold")}>{studentId}</span>
            <button
              type="button"
              aria-label="Copy student ID"
              title="Copy"
              onClick={() => copyText(studentId, "Copied student ID")}
              className="flex size-7 items-center justify-center rounded-md text-ink-3 hover:bg-soft"
            >
              <Copy className="h-3.5 w-3.5" />
            </button>
          </span>
        </div>
      </div>
      <dl className="flex flex-wrap gap-x-7 gap-y-2 phone:grid phone:w-full phone:grid-cols-2">
        {facts.map(([k, v, tone]) => (
          <div key={k} className="flex flex-col gap-0.5">
            <dt className="whitespace-nowrap text-xs text-ink-3">{k}</dt>
            <dd className="flex items-center gap-1.5 whitespace-nowrap text-[13px] font-medium">
              {tone && <Dot tone={tone} />}
              {v}
            </dd>
          </div>
        ))}
      </dl>
      <span className="ml-auto">
        {live ? (
          <Pill tone="success">
            <span aria-hidden="true" className="size-[7px] rounded-full bg-dot-ok motion-safe:animate-pulse" />
            Live
          </Pill>
        ) : (
          <Pill dot="off">Paused · last update {updatedAt ? clock(new Date(updatedAt), true) : "—"}</Pill>
        )}
      </span>
    </div>
  );
}
