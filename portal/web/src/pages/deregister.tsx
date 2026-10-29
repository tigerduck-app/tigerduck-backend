// Deregister: find a student by id or name, see what goes with the account,
// then delete it. The delete itself is by student id.

import { useRef, useState } from "react";
import { useMutation, useQuery } from "@tanstack/react-query";
import { toast } from "sonner";
import { Check, ChevronRight, Download, TriangleAlert, UserMinus } from "lucide-react";
import { api } from "@/lib/api";
import { cn } from "@/lib/cn";
import { ago, stamp } from "@/lib/format";
import { Button } from "@/components/ui/button";
import { Skeleton } from "@/components/ui/skeleton";
import { DialogIcon, ErrorAlert, FormDialog, PageHeading, fieldClass } from "@/components/page";
import { errorText } from "./lists/format";
import { Initial, StudentCard, fmt, totalRows, type Snapshot } from "./deregister/StudentCard";

type Candidate = {
  student_id: string;
  display_name: string | null;
  devices: number;
  last_active: string | null;
  joined: string | null;
};
type Preview = Snapshot & { display_name: string | null; last_active: string | null };
type Report = Snapshot & { deleted: boolean; timestamp: string; user_id: string };

function download(report: Report) {
  const name = `deregister-${report.student_id}-${new Date().toISOString().slice(0, 10)}.json`;
  const url = URL.createObjectURL(
    new Blob([JSON.stringify(report, null, 2)], { type: "application/json" }),
  );
  const a = document.createElement("a");
  a.href = url;
  a.download = name;
  a.click();
  URL.revokeObjectURL(url);
  toast.success(`Saved ${name}`);
}

const devicesLabel = (n: number) => `${n} ${n === 1 ? "device" : "devices"}`;

export function DeregisterPage() {
  const [query, setQuery] = useState("");
  const [missed, setMissed] = useState<string | null>(null);
  const [candidates, setCandidates] = useState<Candidate[] | null>(null);
  const [studentId, setStudentId] = useState<string | null>(null);
  const [confirming, setConfirming] = useState(false);
  const [typed, setTyped] = useState("");
  const [done, setDone] = useState<{ report: Report; person: Preview } | null>(null);
  // The term of the lookup in flight; a reset or an edit drops its answer.
  const asked = useRef<string | null>(null);

  const lookup = useMutation({
    mutationFn: (q: string) =>
      api<{ students: Candidate[] }>(`/api/deregister/lookup?q=${encodeURIComponent(q)}`),
    onSuccess: ({ students }, q) => {
      if (asked.current !== q) return;
      asked.current = null;
      if (students.length === 0) setMissed(q);
      else if (students.length === 1) choose(students[0]);
      else setCandidates(students);
    },
  });
  const previewQ = useQuery<Preview>({
    queryKey: ["deregister-preview", studentId],
    queryFn: () =>
      api<Preview>(`/api/deregister/preview?student_id=${encodeURIComponent(studentId!)}`),
    enabled: studentId !== null && done === null,
    gcTime: 0,
  });
  const deregister = useMutation({
    mutationFn: (id: string) =>
      api<Report>("/api/deregister", { method: "POST", json: { student_id: id } }),
    onSuccess: (report) => {
      setDone({ report, person: previewQ.data! });
      setConfirming(false);
      toast.success(`Deregistered ${report.student_id}`);
    },
  });

  function choose(c: Candidate) {
    setCandidates(null);
    setQuery(c.student_id);
    setStudentId(c.student_id);
  }
  function reset() {
    asked.current = null;
    setQuery("");
    setMissed(null);
    setStudentId(null);
    setDone(null);
    lookup.reset();
  }
  const term = query.trim();
  const preview = previewQ.data;
  const person = done?.person ?? preview;
  const rows = done ? totalRows(done.report) : preview ? totalRows(preview) : 0;

  return (
    <>
      <PageHeading
        group="Devices"
        title="Deregister student"
        meta={
          <span className="font-sans">
            Permanent · account, devices, sessions, credentials, sync state and push jobs
          </span>
        }
      />

      {done && (
        <div
          role="status"
          className="flex flex-wrap items-center gap-3 rounded-[14px] border border-badge-success-fg/25 bg-badge-success px-[18px] py-3.5 text-badge-success-fg"
        >
          <span
            aria-hidden="true"
            className="flex size-8 items-center justify-center rounded-full bg-badge-success-fg text-badge-success"
          >
            <Check className="h-4 w-4" strokeWidth={2.6} />
          </span>
          <div className="flex flex-col">
            <span className="font-semibold">Deregistered {done.report.student_id}</span>
            <span className="text-[13px] tabular-nums">
              {stamp(done.report.timestamp)} · {fmt(rows)} rows · {devicesLabel(done.report.devices.length)}
            </span>
          </div>
          <div className="ml-auto flex flex-wrap gap-2">
            <Button
              variant="line"
              size="bar"
              className="gap-1.5 border-badge-success-fg/25 font-normal"
              onClick={() => download(done.report)}
            >
              <Download className="!size-[15px]" />
              Download JSON
            </Button>
            <Button variant="ink" size="bar" className="font-normal" onClick={reset}>
              Deregister another
            </Button>
          </div>
        </div>
      )}

      {!done && (
        <section
          aria-labelledby="find-h"
          className="flex flex-col gap-3 rounded-[14px] border border-line bg-surface px-6 pb-5 pt-[18px] phone:px-4"
        >
          <h2 id="find-h" className="text-[15px] font-semibold">
            Find student
          </h2>
          <form
            className="flex flex-wrap items-end gap-2.5"
            onSubmit={(e) => {
              e.preventDefault();
              if (!term) return;
              setMissed(null);
              setStudentId(null);
              asked.current = term;
              lookup.mutate(term);
            }}
          >
            <label className="flex flex-[0_1_320px] flex-col gap-1.5 text-[13px] font-medium phone:flex-[1_1_100%]">
              Student ID or name
              <input
                value={query}
                onChange={(e) => {
                  asked.current = null;
                  setQuery(e.target.value);
                  setMissed(null);
                }}
                maxLength={32}
                placeholder="B99900000 or 王小明"
                aria-invalid={missed !== null}
                aria-describedby={missed !== null ? "find-err" : undefined}
                autoFocus
                className={cn(fieldClass, "font-mono font-normal", missed !== null && "border-dot-bad")}
              />
            </label>
            <Button type="submit" variant="ink" size="form" disabled={!term || lookup.isPending}>
              Look up
            </Button>
          </form>
          {missed !== null && (
            <span id="find-err" role="alert" className="flex items-center gap-1.5 text-[13px] text-badge-danger-fg">
              <TriangleAlert className="h-[15px] w-[15px]" />
              No student matches {missed}
            </span>
          )}
          {lookup.isError && <ErrorAlert>{errorText(lookup.error)}</ErrorAlert>}
        </section>
      )}

      {!done && previewQ.isLoading && <Skeleton className="h-72" />}
      {!done && previewQ.isError && (
        <ErrorAlert className="items-center" onRetry={() => previewQ.refetch()}>
          {errorText(previewQ.error)}
        </ErrorAlert>
      )}
      {person && (
        <StudentCard
          snapshot={done?.report ?? person}
          name={person.display_name}
          lastActive={person.last_active}
          report={!!done}
          footer={
            !done && (
              <>
                <Button variant="line" size="form" onClick={reset}>
                  Cancel
                </Button>
                <Button
                  variant="danger"
                  size="form"
                  className="gap-2"
                  onClick={() => {
                    deregister.reset();
                    setTyped("");
                    setConfirming(true);
                  }}
                >
                  <UserMinus className="!size-[15px]" />
                  Deregister {person.student_id}
                </Button>
              </>
            )
          }
        />
      )}

      <FormDialog
        open={candidates !== null}
        onOpenChange={(o) => !o && setCandidates(null)}
        width={560}
        title="Pick a student"
        meta={candidates && `${candidates.length} matches`}
      >
        <div className="flex flex-col gap-1.5">
          {candidates?.map((c) => (
            <button
              key={c.student_id}
              type="button"
              onClick={() => choose(c)}
              className="flex min-h-[60px] w-full items-center gap-3 rounded-xl border border-line bg-surface px-3 py-2.5 text-left hover:bg-row-hover"
            >
              <Initial name={c.display_name} id={c.student_id} className="size-9 bg-soft" />
              <span className="flex min-w-0 flex-1 flex-col gap-0.5">
                <span className="flex flex-wrap items-baseline gap-x-2.5">
                  <span className="font-mono text-[15px] font-medium">{c.student_id}</span>
                  {c.display_name && <span className="font-medium">{c.display_name}</span>}
                </span>
                <span className="text-xs tabular-nums text-ink-3">
                  {[
                    devicesLabel(c.devices),
                    c.last_active && `last active ${ago(c.last_active)}`,
                    c.joined && `joined ${stamp(c.joined).slice(0, 10)}`,
                  ]
                    .filter(Boolean)
                    .join(" · ")}
                </span>
              </span>
              <ChevronRight aria-hidden="true" className="h-4 w-4 shrink-0 text-side-muted" />
            </button>
          ))}
        </div>
      </FormDialog>

      {preview && (
        <FormDialog
          open={confirming}
          onOpenChange={(o) => !o && !deregister.isPending && setConfirming(false)}
          width={560}
          icon={<DialogIcon icon={TriangleAlert} danger round />}
          title="Delete this student permanently?"
          meta="Cannot be undone"
          footer={
            <>
              <Button
                variant="line"
                size="form"
                disabled={deregister.isPending}
                onClick={() => setConfirming(false)}
              >
                Cancel
              </Button>
              <Button
                variant="danger"
                size="form"
                disabled={typed.trim().toUpperCase() !== preview.student_id.toUpperCase() || deregister.isPending}
                onClick={() => deregister.mutate(preview.student_id)}
              >
                {deregister.isPending ? "Deleting…" : "Delete permanently"}
              </Button>
            </>
          }
        >
          <div className="flex items-center gap-3 rounded-xl bg-ground px-3.5 py-3">
            <Initial name={preview.display_name} id={preview.student_id} className="size-9 bg-line" />
            <div className="flex min-w-0 flex-col">
              <span className="font-mono text-[15px] font-medium">{preview.student_id}</span>
              {preview.display_name && <span className="text-[13px] text-ink-2">{preview.display_name}</span>}
            </div>
          </div>
          <div className="grid grid-cols-3 gap-2 text-badge-danger-fg">
            {[
              ["Devices", String(preview.devices.length)],
              ["Rows", fmt(rows)],
              ["School sign-in", "Removed"],
            ].map(([label, value]) => (
              <div key={label} className="flex min-w-0 flex-col rounded-[10px] bg-badge-danger px-3 py-2.5">
                <span className="text-xs">{label}</span>
                <span className="text-lg font-semibold tabular-nums">{value}</span>
              </div>
            ))}
          </div>
          <label className="flex flex-col gap-1.5 text-[13px] font-medium">
            Type {preview.student_id} to confirm
            <input
              value={typed}
              onChange={(e) => setTyped(e.target.value)}
              autoComplete="off"
              placeholder={preview.student_id}
              className={cn(fieldClass, "font-mono font-normal")}
            />
          </label>
          {deregister.isError && <ErrorAlert>{errorText(deregister.error)}</ErrorAlert>}
        </FormDialog>
      )}
    </>
  );
}
