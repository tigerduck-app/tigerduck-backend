// Data inspection's dialogs: cancelling the queue, the test sign-in expired
// notice and the test Live Activity. The routes are in moodle/push.py and
// moodle/manual_tests.py.

import { useState, type ReactNode } from "react";
import { BellRing, Check, Info, TriangleAlert, X } from "lucide-react";
import { Button } from "@/components/ui/button";
import { FormDialog, fieldClass } from "@/components/page";
import { cn } from "@/lib/cn";
import { Segmented } from "./controls";
import { clock, deviceNoun, pushLabel } from "./format";
import { kindOf } from "./DevicePanel";
import { takesPush } from "./Topology";
import type { QueuedJob, SyncDevice } from "./types";

function Who({ studentId, name, extra }: { studentId: string; name: string | null; extra?: string }) {
  return (
    <span className="flex flex-wrap gap-x-1.5">
      <span className="font-mono">{studentId}</span>
      {name && <span>{name}</span>}
      {extra && <span>{extra}</span>}
    </span>
  );
}

const GROUP = { la: "Live Activity", rem: "Reminder", sys: "System" } as const;

/** What a queued job is, short enough to list beside others of its group. */
function shortLabel(j: QueuedJob): string {
  if (j.channel === "schedule") return j.kind === "live_activity_end" ? "end" : pushLabel(j).replace("Live Activity start", "Start");
  if (j.channel === "course") return "Class";
  if (j.channel === "assignment") return "assignment due";
  return pushLabel(j);
}

export function ClearQueueDialog({
  jobs,
  total,
  studentId,
  name,
  pending,
  onClose,
  onConfirm,
}: {
  jobs: QueuedJob[];
  total: number;
  studentId: string;
  name: string | null;
  pending: boolean;
  onClose: () => void;
  onConfirm: () => void;
}) {
  const groups = new Map<string, QueuedJob[]>();
  for (const j of jobs) {
    const kind = kindOf(j);
    const label = kind ? GROUP[kind as keyof typeof GROUP] : pushLabel(j);
    groups.set(label, [...(groups.get(label) ?? []), j]);
  }
  const n = total;
  return (
    <FormDialog
      open
      onOpenChange={(open) => !open && onClose()}
      title={
        <span className="flex items-center gap-3">
          <span aria-hidden="true" className="flex size-9 shrink-0 items-center justify-center rounded-[10px] bg-badge-danger text-badge-danger-fg">
            <TriangleAlert className="h-[18px] w-[18px]" />
          </span>
          Cancel {n} queued {n === 1 ? "push" : "pushes"}?
        </span>
      }
      meta={<Who studentId={studentId} name={name} extra="· every device" />}
      width={520}
      footer={
        <>
          <Button variant="line" size="form" onClick={onClose}>
            Keep them
          </Button>
          <Button variant="danger" size="form" disabled={pending} onClick={onConfirm}>
            Cancel {n} {n === 1 ? "push" : "pushes"}
          </Button>
        </>
      }
    >
      <ul className="flex flex-col rounded-xl border border-line">
        {[...groups].map(([label, list]) => (
          <li key={label} className="flex items-center gap-3 border-b border-divider px-4 py-2.5 last:border-b-0">
            <span className="font-semibold">{label}</span>
            <span className="min-w-0 flex-1 truncate text-[13px] text-ink-3">
              {[...new Set(list.map(shortLabel))].join(", ")}
            </span>
            <span className="font-semibold tabular-nums">{list.length}</span>
          </li>
        ))}
      </ul>
    </FormDialog>
  );
}

export function ReauthDialog({
  devices,
  studentId,
  name,
  pending,
  onClose,
  onConfirm,
}: {
  devices: SyncDevice[];
  studentId: string;
  name: string | null;
  pending: boolean;
  onClose: () => void;
  onConfirm: () => void;
}) {
  const reached = devices.filter(takesPush).length;
  return (
    <FormDialog
      open
      onOpenChange={(open) => !open && onClose()}
      title="Send a test sign-in expired notice?"
      meta={<Who studentId={studentId} name={name} />}
      width={520}
      footer={
        <>
          <Button variant="line" size="form" onClick={onClose}>
            Cancel
          </Button>
          <Button variant="ink" size="form" disabled={pending || reached === 0} onClick={onConfirm}>
            Send to {reached} {reached === 1 ? "device" : "devices"}
          </Button>
        </>
      }
    >
      <div className="flex flex-col gap-3 rounded-xl border border-line p-4">
        <div className="flex items-center gap-2.5">
          <span aria-hidden="true" className="flex size-8 items-center justify-center rounded-[9px] bg-badge-warning text-badge-warning-fg">
            <BellRing className="h-4 w-4" />
          </span>
          <span className="font-semibold">Moodle sign-in expired</span>
          <span className="ml-auto text-xs text-ink-3">each device’s language</span>
        </div>
        <ul className="flex flex-col gap-1.5">
          {devices.map((d) => {
            const ok = takesPush(d);
            return (
              <li key={d.id} className={cn("flex items-center gap-2 text-[13px]", !ok && "text-ink-3")}>
                {ok ? <Check className="h-4 w-4 text-dot-ok" /> : <X className="h-4 w-4" />}
                <span className="font-medium">{deviceNoun(d.platform)}</span>
                <span className="font-mono text-xs text-ink-3">
                  {!ok ? (d.platform === "macos" ? "No push" : "no push token") : d.locale ?? "—"}
                </span>
              </li>
            );
          })}
        </ul>
      </div>
      <span className="flex items-center gap-2 text-[13px] text-ink-3">
        <Info className="h-4 w-4 shrink-0" />
        Sign-in and sync jobs stay as they are
      </span>
    </FormDialog>
  );
}

const SCENARIOS = [
  { value: "inClass", label: "In class", short: "上課" },
  { value: "classPreparing", label: "Starting soon", short: "即將上課" },
  { value: "assignmentUrgent", label: "Assignment due", short: "作業" },
] as const;
type Scenario = (typeof SCENARIOS)[number]["value"];

export type LiveActivityBody = {
  scenario: Scenario;
  title: string;
  subtitle: string;
  location: string;
  minutes: number;
};

function Labeled({ label, children, error, className }: { label: ReactNode; children: ReactNode; error?: string | false; className?: string }) {
  return (
    <label className={cn("flex flex-col gap-1.5 text-[13px] font-medium", className)}>
      {label}
      {children}
      {error && <span className="text-xs font-normal text-badge-danger-fg">{error}</span>}
    </label>
  );
}

export function LiveActivityDialog({
  device,
  pending,
  onClose,
  onConfirm,
}: {
  device: SyncDevice;
  pending: boolean;
  onClose: () => void;
  onConfirm: (body: LiveActivityBody) => void;
}) {
  const [scenario, setScenario] = useState<Scenario>("inClass");
  const [title, setTitle] = useState("Portal test");
  const [subtitle, setSubtitle] = useState("Live Activity test");
  const [location, setLocation] = useState("");
  const [minutes, setMinutes] = useState("2");
  const m = Number(minutes);
  const minutesBad = !(Number.isInteger(m) && m >= 1 && m <= 240);
  const titleMissing = title.trim().length === 0;
  const short = SCENARIOS.find((s) => s.value === scenario)!.short;
  const countdown = minutesBad ? "—" : m >= 60 ? `${Math.floor(m / 60)}:${String(m % 60).padStart(2, "0")}:00` : `${m}:00`;

  return (
    <FormDialog
      open
      onOpenChange={(open) => !open && onClose()}
      title="Start a test Live Activity"
      meta={
        <span className="flex gap-1.5">
          <span>{deviceNoun(device.platform)}</span>
          <span className="font-mono">{device.client_device_id.slice(0, 8)}</span>
        </span>
      }
      width={760}
      footer={
        <>
          <Button variant="line" size="form" onClick={onClose}>
            Cancel
          </Button>
          <Button
            variant="ink"
            size="form"
            disabled={pending || minutesBad || titleMissing}
            onClick={() => onConfirm({ scenario, title: title.trim(), subtitle: subtitle.trim(), location: location.trim(), minutes: m })}
          >
            Start Live Activity
          </Button>
        </>
      }
    >
      <div className="flex flex-wrap gap-6 phone:flex-col">
        <div className="flex min-w-0 flex-[1_1_360px] flex-col gap-3.5 phone:flex-auto">
          <div className="flex flex-col gap-1.5">
            <span id="sc-label" className="text-[13px] font-medium">
              Scenario
            </span>
            <Segmented
              radio
              label="Scenario"
              value={scenario}
              onChange={setScenario}
              options={SCENARIOS.map((s) => ({ value: s.value, label: s.label }))}
              className="rounded-[10px]"
              itemClassName="min-h-9 flex-1 rounded-lg px-2"
            />
          </div>
          <Labeled label="Title" error={titleMissing && "Required"}>
            <input
              value={title}
              maxLength={120}
              onChange={(e) => setTitle(e.target.value)}
              className={cn(fieldClass, "font-normal", titleMissing && "border-dot-bad")}
            />
          </Labeled>
          <Labeled label="Subtitle">
            <input value={subtitle} maxLength={120} onChange={(e) => setSubtitle(e.target.value)} className={cn(fieldClass, "font-normal")} />
          </Labeled>
          <div className="flex flex-wrap gap-3">
            <Labeled
              className="flex-[1_1_140px]"
              label={
                <span>
                  Location <span className="font-normal text-ink-3">optional</span>
                </span>
              }
            >
              <input
                value={location}
                maxLength={60}
                placeholder="TR-313"
                onChange={(e) => setLocation(e.target.value)}
                className={cn(fieldClass, "font-mono font-normal")}
              />
            </Labeled>
            <Labeled className="flex-[0_1_140px]" label="Stays up" error={minutesBad && "1 to 240"}>
              <span className="flex items-center gap-2">
                <input
                  type="number"
                  min={1}
                  max={240}
                  value={minutes}
                  onChange={(e) => setMinutes(e.target.value)}
                  className={cn(fieldClass, "w-20 font-normal tabular-nums", minutesBad && "border-dot-bad")}
                />
                <span className="font-normal text-ink-3">min</span>
              </span>
            </Labeled>
          </div>
        </div>
        <div className="flex min-w-0 flex-[1_1_280px] flex-col gap-3 phone:flex-auto">
          <span className="text-[13px] font-medium">Preview</span>
          <div role="img" aria-label="Lock screen preview" className="flex flex-col gap-3 rounded-[20px] bg-[#1B1E24] p-4 text-white">
            <div className="flex items-center gap-2.5">
              <span aria-hidden="true" className="flex size-[34px] items-center justify-center rounded-[9px] bg-brand font-bold text-[#14161A]">
                T
              </span>
              <div className="flex min-w-0 flex-1 flex-col">
                <span className="truncate font-semibold">{title.trim() || "Title"}</span>
                <span className="truncate text-xs text-side-ink">{subtitle.trim() || " "}</span>
              </div>
              <div className="flex flex-col items-end">
                <span className="text-xl font-semibold tabular-nums">{countdown}</span>
                <span className="text-[11px] text-side-ink">{short}</span>
              </div>
            </div>
            <div aria-hidden="true" className="h-1.5 rounded-full bg-[#343944]">
              <div className="h-1.5 w-[8%] rounded-full bg-brand" />
            </div>
            <div className="flex justify-between text-xs text-side-ink">
              <span className="font-mono">{location.trim() || "—"}</span>
              <span className="tabular-nums">{minutesBad ? "" : `Ends ${clock(new Date(Date.now() + m * 60_000))}`}</span>
            </div>
          </div>
          <div className="flex flex-col gap-1.5">
            <span className="text-xs text-ink-3">Needs on the phone</span>
            <div className="flex flex-wrap gap-1.5">
              {["Fake clock off", "Live Updates on", "TigerSync on"].map((t) => (
                <span key={t} className="rounded-full bg-soft px-[9px] py-0.5 text-xs text-ink-2">
                  {t}
                </span>
              ))}
            </div>
          </div>
        </div>
      </div>
    </FormDialog>
  );
}
