// One device of the account: what it syncs, the account data it sees, the
// pushes it got, what the server has queued for it and the pushes it caused.

import { useState, type ReactNode } from "react";
import { Check, Copy, TriangleAlert, X } from "lucide-react";
import { toast } from "sonner";
import { Button } from "@/components/ui/button";
import { Skeleton } from "@/components/ui/skeleton";
import { DataTable, useTable, type Column, type TableModel } from "@/components/data-table";
import { FormDialog, Pill } from "@/components/page";
import { copyText } from "@/lib/copy";
import { cn } from "@/lib/cn";
import { monthDay, monthDayTime } from "@/lib/format";
import { platformLabel } from "@/lib/platform";
import { assignmentColumns, colorOf, deviceName, Swatches, termColumn } from "./BackendPanel";
import { Chips, Segmented, TabBar } from "./controls";
import { ago, clock, deviceNoun, errorText, post, pushLabel, StatusPill, until } from "./format";
import { DeviceIcon, deviceBadges, isOnline, queuedFor, takesPush } from "./Topology";
import type { PushDeliveryRow, QueuedJob, SyncCoursesResponse, SyncDevice, SyncEventsResponse } from "./types";

type Tab = "synced" | "data" | "push" | "queued" | "source" | "info" | "tests";

export function DevicePanel({
  device,
  events,
  courses,
  coursesError,
  studentId,
  onLiveActivity,
}: {
  device: SyncDevice;
  events: SyncEventsResponse;
  courses?: SyncCoursesResponse;
  /** The alert for a failed course load; the synced list and data tabs show it. */
  coursesError?: ReactNode;
  studentId: string;
  onLiveActivity: () => void;
}) {
  const [tab, setTab] = useState<Tab>("synced");
  const queued = queuedFor(events.queued_jobs ?? [], device.id);
  const deliveries = (events.push_deliveries ?? []).filter((d) => d.device_id === device.id);
  const sourced = (events.push_jobs ?? []).filter((j) => j.source_device_id === device.id);
  // Live Activities exist only on an iPhone or iPad.
  const liveActivity = device.platform === "ios" || device.platform === "ipados";
  const shown = tab === "tests" && !liveActivity ? "synced" : tab;
  const online = isOnline(device);

  return (
    <section aria-labelledby="dev-title" className="min-w-0 rounded-[14px] border border-line bg-surface">
      <div className="flex flex-col gap-3 border-b border-line px-6 pt-4 phone:px-4">
        <div className="flex flex-wrap items-center gap-2.5">
          <span aria-hidden="true" className="flex size-8 items-center justify-center rounded-[9px] bg-ground text-ink-2">
            <DeviceIcon platform={device.platform} />
          </span>
          <h2 id="dev-title" className="text-[15px] font-semibold">
            {deviceNoun(device.platform)}
          </h2>
          <span className="font-mono text-xs text-ink-3">{device.client_device_id.slice(0, 8)}</span>
          <Pill tone={online ? "success" : "neutral"} dot={online ? "ok" : "off"}>
            {online ? "Online" : `Offline · ${ago(device.last_seen_at)}`}
          </Pill>
          {deviceBadges(device, queued.length)}
          <span className="ml-auto whitespace-nowrap text-xs text-ink-3">
            {[device.app_version, device.os_version && `${platformLabel(device.platform)} ${device.os_version}`]
              .filter(Boolean)
              .join(" · ")}
          </span>
        </div>
        <TabBar
          label="Device data"
          value={shown}
          onChange={setTab}
          className="flex-wrap gap-0.5 phone:flex-nowrap phone:overflow-x-auto phone:[scrollbar-width:none]"
          tabs={[
            { value: "synced", label: "Synced list" },
            { value: "data", label: "Data" },
            { value: "push", label: "Push", count: deliveries.length },
            { value: "queued", label: "Queued", count: queued.length },
            { value: "source", label: "Source push", count: sourced.length },
            { value: "info", label: "Info" },
            ...(liveActivity ? [{ value: "tests" as const, label: "Tests" }] : []),
          ]}
        />
      </div>
      {coursesError && (shown === "synced" || shown === "data") && <div className="px-6 pt-4 phone:px-4">{coursesError}</div>}
      {shown === "synced" && (courses || !coursesError) && <SyncedList device={device} courses={courses} />}
      {shown === "data" &&
        (courses ? <DataTabs device={device} events={events} courses={courses} /> : !coursesError && <Skeleton className="m-6 h-40" />)}
      {shown === "push" && <PushTable device={device} deliveries={deliveries} />}
      {shown === "queued" && <QueuedList jobs={queued} deviceId={device.id} studentId={studentId} />}
      {shown === "source" && (
        <div className="flex flex-col gap-2.5 px-6 pb-5 pt-4 phone:px-4">
          {sourced.length === 0 && <div className="p-6 text-center font-semibold">Nothing here</div>}
          {sourced.map((j) => {
            const to = (events.push_deliveries ?? []).filter((d) => d.push_job_id === j.id);
            const fired = j.sent_at ?? j.fire_at;
            return (
              <div key={j.id} className="flex flex-col gap-2.5 rounded-xl border border-line px-3.5 py-3">
                <div className="flex flex-wrap items-center gap-2">
                  <StatusPill status={j.status} dot={false} />
                  <span className="font-semibold">{pushLabel({ channel: "system", scenario: j.scenario, kind: null })}</span>
                  <span className="ml-auto whitespace-nowrap text-xs tabular-nums text-ink-3">
                    #{j.id} · {j.attempts}/{j.max_attempts} · {j.status === "pending" ? "fires" : "fired"} {clock(fired)}
                  </span>
                </div>
                {to.length > 0 && (
                  <div className="flex flex-wrap items-center gap-2">
                    <span className="text-xs text-ink-3">Delivered to</span>
                    {to.map((d) => {
                      const ok = d.status === "sent";
                      return (
                        <Pill key={d.id} tone={ok ? "success" : "neutral"}>
                          {ok ? <Check className="h-3 w-3" /> : <X className="h-3 w-3" />}
                          {deviceName(events.devices ?? [], d.device_id) ?? "—"}
                        </Pill>
                      );
                    })}
                  </div>
                )}
              </div>
            );
          })}
        </div>
      )}
      {shown === "info" && <Info device={device} />}
      {shown === "tests" && (
        <div className="px-6 pb-6 pt-5 phone:px-4">
          <div className="flex flex-wrap items-center justify-between gap-4 rounded-xl border border-line px-[18px] py-4">
            <div className="flex items-center gap-3">
              <span aria-hidden="true" className="flex h-6 w-[52px] shrink-0 items-center justify-between rounded-full bg-[#15171A] px-2">
                <span className="size-2 rounded-full bg-brand" />
                <span className="h-1 w-[18px] rounded-full bg-white" />
              </span>
              <div className="flex flex-col gap-0.5">
                <span className="font-semibold">Live Activity</span>
                <span className="text-xs text-ink-3">Push-to-start, ends on its own</span>
              </div>
            </div>
            <Button variant="line" size="form" onClick={onLiveActivity}>
              Start a test
            </Button>
          </div>
        </div>
      )}
    </section>
  );
}

// ── Synced list ────────────────────────────────────────────────────────

type Row = { label: string; on: boolean; has: boolean; detail: string; child?: boolean };

const asRecord = (v: unknown) =>
  v && typeof v === "object" && !Array.isArray(v) ? (v as Record<string, unknown>) : undefined;

function lead(mins: number): string {
  if (mins % 1440 === 0) return `${mins / 1440} day${mins === 1440 ? "" : "s"}`;
  return mins % 60 === 0 ? `${mins / 60} h` : `${mins} min`;
}

/**
 * The offsets the reminder scan reads (server/push/reminders.py): a minutes
 * list whenever present, else the hours list older apps write.
 */
function offsets(section: Record<string, unknown> | undefined): string {
  if (!section) return "—";
  if (section.enabled === false) return "Off";
  const raw = Array.isArray(section.reminder_offsets_minutes)
    ? section.reminder_offsets_minutes
    : Array.isArray(section.reminder_offsets_hours)
      ? section.reminder_offsets_hours.map((h) => (typeof h === "number" ? h * 60 : h))
      : null;
  if (!raw) return "—";
  const mins = raw.filter((n): n is number => typeof n === "number").sort((a, b) => b - a);
  if (mins.length === 0) return "Off";
  const words = mins.map(lead);
  return `${words.length > 1 ? `${words.slice(0, -1).join(", ")} and ${words.at(-1)}` : words[0]} before`;
}

function liveKinds(section: Record<string, unknown> | undefined): string {
  if (!section) return "—";
  const kinds = [
    section.show_in_class !== false && "In class",
    section.show_class_preparing !== false && "Starting soon",
    section.show_assignment !== false && "Assignment due",
  ].filter(Boolean);
  return kinds.length ? kinds.join(", ") : "Off";
}

function SyncedList({ device, courses }: { device: SyncDevice; courses?: SyncCoursesResponse }) {
  const list = courses?.courses ?? [];
  const assignments = courses?.assignments ?? [];
  const notification = asRecord(courses?.settings_documents?.find((d) => d.namespace === "notification")?.document);
  const colored = list.filter((c) => c.color_hex).length;
  const renamed = list.filter((c) => Object.keys(c.custom_names ?? {}).length > 0).length;
  const master = device.cloud_sync_enabled !== false;
  const groups: { title: string; rows: Row[] }[] = [
    {
      title: "同步課程資訊",
      rows: [
        { label: "同步課程資訊", on: master, has: list.length + assignments.length > 0, detail: master ? "On · the rows below apply" : "Off · course data stays on this device" },
        { label: "Assignment status", on: device.sync_assignments !== false, has: assignments.length > 0, detail: `${assignments.length} assignments in backend`, child: true },
        { label: "Assignment due reminders", on: device.sync_assignment_reminders !== false, has: !!asRecord(notification?.assignments), detail: offsets(asRecord(notification?.assignments)), child: true },
        { label: "Live Activity / Live Updates", on: device.sync_live_activity !== false, has: !!asRecord(notification?.live_activity), detail: liveKinds(asRecord(notification?.live_activity)), child: true },
        { label: "Class table, all courses", on: device.sync_courses !== false, has: list.length > 0, detail: `${list.length} courses in backend`, child: true },
        { label: "Course colours", on: device.sync_course_colors !== false, has: colored > 0, detail: `${colored} of ${list.length} synced`, child: true },
        { label: "Custom course names", on: device.sync_course_names !== false, has: renamed > 0, detail: `${renamed} of ${list.length} renamed`, child: true },
      ],
    },
    {
      title: "Push",
      rows: [
        { label: "接收額外伺服器推播", on: device.server_push_enabled !== false, has: false, detail: "Operator custom pushes" },
        { label: "Bulletin pushes", on: device.bulletin_push_enabled !== false, has: false, detail: "New bulletins matching a subscription" },
      ],
    },
  ];
  const grid = "grid grid-cols-[minmax(200px,1.3fr)_96px_112px_minmax(160px,1fr)] items-center gap-3 tablet:grid-cols-[minmax(160px,1.3fr)_88px_104px_minmax(0,1fr)] phone:grid-cols-[minmax(0,1fr)_auto]";
  if (!courses) return <Skeleton className="m-6 h-40" />;
  return (
    <div className="flex flex-col gap-3.5 px-6 pb-5 pt-2 phone:px-4">
      {groups.map((g) => (
        <div key={g.title} className="flex flex-col">
          <div className={cn(grid, "border-b border-line pb-1.5 pt-2.5 text-xs font-medium text-ink-3")}>
            <span>{g.title}</span>
            <span className="whitespace-nowrap">This device</span>
            <span className="whitespace-nowrap phone:hidden">Account data</span>
            <span className="phone:hidden">Detail</span>
          </div>
          {g.rows.map((r) => (
            <div
              key={r.label}
              className={cn(grid, "min-h-10 border-b border-divider py-1", r.child && !master && "opacity-50")}
            >
              <span className={r.child ? "pl-4" : "font-semibold"}>{r.label}</span>
              <span className="flex items-center gap-1.5" aria-label={r.on ? "On" : "Off"}>
                <span
                  aria-hidden="true"
                  className={cn(
                    "flex h-[18px] w-[30px] rounded-full p-0.5",
                    r.on ? "justify-end bg-ink" : "justify-start bg-field",
                  )}
                >
                  <span className="size-3.5 rounded-full bg-surface" />
                </span>
                <span aria-hidden="true" className="whitespace-nowrap text-xs text-ink-2">
                  {r.on ? "On" : "Off"}
                </span>
              </span>
              <span className="flex items-center gap-1.5 whitespace-nowrap text-xs text-ink-2 phone:hidden">
                <span aria-hidden="true" className={cn("size-2 rounded-full", r.has ? "bg-dot-ok" : "bg-disabled")} />
                {r.has ? "Has data" : "None"}
              </span>
              <span className="text-[13px] text-ink-2 phone:hidden">{r.detail}</span>
            </div>
          ))}
        </div>
      ))}
    </div>
  );
}

// ── Data ───────────────────────────────────────────────────────────────

type Sub = "courses" | "names" | "assignments" | "holidays" | "skipped" | "settings" | "bulletins";

const dash = (v: string | null | undefined) => (v ? v : <span className="text-ink-3">—</span>);
const onOff = (on: boolean) => <Pill tone={on ? "success" : "neutral"}>{on ? "On" : "Off"}</Pill>;

function DataTabs({ device, events, courses }: { device: SyncDevice; events: SyncEventsResponse; courses: SyncCoursesResponse }) {
  const [sub, setSub] = useState<Sub>("courses");
  const devices = events.devices ?? [];
  const named = courses.courses.filter((c) => Object.keys(c.custom_names ?? {}).length > 0);
  const subs = courses.bulletin_subscriptions?.filter((s) => s.device_id === device.id) ?? [];
  const by = (id: string | null) => deviceName(devices, id);
  const [doc, setDoc] = useState<SettingsDoc | null>(null);

  const tables = {
    courses: useTable({
      rows: courses.courses,
      initialSort: { key: "code", dir: "asc" },
      columns: [
        {
          key: "color",
          label: "Color",
          cell: (c) => {
            const color = colorOf(c, courses);
            return (
              <div className="flex items-center gap-1.5 font-mono text-[13px]">
                <Swatches colors={color.swatches.slice(0, 1)} />
                {color.swatches[0]}
              </div>
            );
          },
        },
        { key: "code", label: "Code", sortValue: (c) => c.client_course_no, className: "font-mono text-[13px]", cell: (c) => c.client_course_no },
        { key: "name", label: "Name", sortValue: (c) => c.course_name, cell: (c) => <div title={c.course_name} className="max-w-[240px] truncate phone:max-w-[100px]">{c.course_name}</div> },
        termColumn((c) => c.semester),
      ] satisfies Column<SyncCoursesResponse["courses"][number]>[],
    }),
    names: useTable({
      rows: named,
      initialSort: { key: "code", dir: "asc" },
      columns: [
        { key: "code", label: "Code", sortValue: (c) => c.client_course_no, className: "font-mono text-[13px]", cell: (c) => c.client_course_no },
        { key: "zh", label: "Chinese", sortValue: (c) => c.custom_names?.zh ?? "", cell: (c) => dash(c.custom_names?.zh) },
        { key: "en", label: "English", sortValue: (c) => c.custom_names?.en ?? "", cell: (c) => dash(c.custom_names?.en) },
        termColumn((c) => c.semester),
      ] satisfies Column<SyncCoursesResponse["courses"][number]>[],
    }),
    assignments: useTable({
      rows: courses.assignments ?? [],
      initialSort: { key: "due", dir: "desc" },
      columns: assignmentColumns(events.overrides ?? []),
    }),
    holidays: useTable({
      rows: courses.holiday_overrides ?? [],
      initialSort: { key: "dates", dir: "desc" },
      columns: [
        { key: "holiday", label: "Holiday", sortValue: (h) => h.name_zh, cell: (h) => `${h.name_zh} ${h.name_en}` },
        {
          key: "dates",
          label: "Dates",
          sortValue: (h) => h.start_date,
          className: "whitespace-nowrap",
          cell: (h) =>
            h.start_date === h.end_date
              ? monthDay(`${h.start_date}T00:00:00Z`, true)
              : `${monthDay(`${h.start_date}T00:00:00Z`, true)} – ${monthDay(`${h.end_date}T00:00:00Z`, true)}`,
        },
        { key: "reminders", label: "Class reminders", filterValue: (h) => (h.notify ? "On" : "Off"), cell: (h) => onOff(h.notify) },
        { key: "updated", label: "Updated", hide: "phone", sortValue: (h) => h.updated_at ?? "", className: "whitespace-nowrap text-[13px] text-ink-3", cell: (h) => ago(h.updated_at) },
      ] satisfies Column<NonNullable<SyncCoursesResponse["holiday_overrides"]>[number]>[],
    }),
    skipped: useTable({
      rows: courses.course_skipped_dates ?? [],
      initialSort: { key: "date", dir: "desc" },
      columns: [
        { key: "date", label: "Date", sortValue: (s) => s.skipped_on, className: "whitespace-nowrap", cell: (s) => monthDay(`${s.skipped_on}T00:00:00Z`, true) },
        { key: "course", label: "Course", sortValue: (s) => s.course_no ?? "", filterValue: (s) => s.course_no, className: "font-mono text-[13px]", cell: (s) => s.course_no ?? "—" },
        { key: "reason", label: "Reason", cell: (s) => <div className="max-w-[240px] truncate">{dash(s.reason)}</div> },
        { key: "by", label: "Set by", hide: "phone", filterValue: (s) => by(s.created_by_device_id), className: "text-[13px] text-ink-3", cell: (s) => by(s.created_by_device_id) ?? "—" },
      ] satisfies Column<NonNullable<SyncCoursesResponse["course_skipped_dates"]>[number]>[],
    }),
    settings: useTable({
      rows: courses.settings_documents ?? [],
      initialSort: { key: "ns", dir: "asc" },
      columns: [
        {
          key: "ns",
          label: "Namespace",
          sortValue: (d) => d.namespace,
          className: "font-mono text-[13px]",
          cell: (d) => (
            <button type="button" onClick={() => setDoc(d)} className="text-link hover:text-link-hover hover:underline">
              {d.namespace}
            </button>
          ),
        },
        { key: "rev", label: "Revision", align: "right", sortValue: (d) => d.revision, className: "font-mono text-[13px]", cell: (d) => d.revision },
        { key: "schema", label: "Schema", hide: "phone", className: "text-[13px] text-ink-3", cell: (d) => `v${d.schema_version}` },
        { key: "updated", label: "Updated", sortValue: (d) => d.updated_at ?? "", className: "whitespace-nowrap text-[13px] text-ink-3", cell: (d) => ago(d.updated_at) },
        { key: "by", label: "By", hide: "phone", filterValue: (d) => by(d.updated_by_device_id), className: "text-[13px] text-ink-3", cell: (d) => by(d.updated_by_device_id) ?? "—" },
      ] satisfies Column<NonNullable<SyncCoursesResponse["settings_documents"]>[number]>[],
    }),
    bulletins: useTable({
      rows: subs,
      initialSort: { key: "name", dir: "asc" },
      columns: [
        { key: "name", label: "Subscription", sortValue: (s) => s.name ?? "", cell: (s) => s.name ?? `#${s.id}` },
        { key: "orgs", label: "Orgs", hide: "phone", cell: (s) => <div className="max-w-[180px] truncate">{dash(s.orgs.join(", "))}</div> },
        { key: "tags", label: "Tags", hide: "phone", cell: (s) => <div className="max-w-[180px] truncate">{dash(s.tags.join(", "))}</div> },
        {
          key: "mode",
          label: "Mode",
          filterValue: (s) => modeLabel(s.mode),
          className: "whitespace-nowrap text-[13px] text-ink-3",
          cell: (s) => modeLabel(s.mode),
        },
        { key: "enabled", label: "Enabled", filterValue: (s) => (s.enabled ? "On" : "Off"), cell: (s) => onOff(s.enabled) },
        { key: "updated", label: "Updated", hide: "phone", sortValue: (s) => s.updated_at ?? "", className: "whitespace-nowrap text-[13px] text-ink-3", cell: (s) => ago(s.updated_at) },
      ] satisfies Column<NonNullable<SyncCoursesResponse["bulletin_subscriptions"]>[number]>[],
    }),
  };
  const labels: Record<Sub, string> = {
    courses: "Courses",
    names: "Custom names",
    assignments: "Assignments",
    holidays: "Holiday overrides",
    skipped: "Skipped dates",
    settings: "Settings",
    bulletins: "Bulletins",
  };
  const subsList = Object.keys(labels) as Sub[];
  const table = tables[sub] as TableModel<unknown>;

  return (
    <>
      <div className="overflow-x-auto border-b border-divider px-6 py-3 phone:px-4">
        <Segmented
          radio={false}
          label="Data kind"
          value={sub}
          onChange={setSub}
          className="inline-flex"
          itemClassName="text-xs"
          options={subsList.map((s) => ({ value: s, label: labels[s], count: tables[s].total }))}
        />
      </div>
      <DataTable
        table={table}
        rowKey={(r) => {
          const row = r as { id?: unknown; holiday_id?: unknown; namespace?: unknown };
          return String(row.id ?? row.holiday_id ?? row.namespace);
        }}
        minWidth={560}
        empty={`No ${labels[sub].toLowerCase()}`}
      />
      {sub === "bulletins" && <BulletinStates courses={courses} />}
      {doc && (
        <FormDialog
          open
          onOpenChange={(open) => !open && setDoc(null)}
          title={<span className="font-mono">{doc.namespace}</span>}
          meta={`Revision ${doc.revision} · v${doc.schema_version} · ${ago(doc.updated_at)}`}
          width={640}
        >
          <pre className="overflow-x-auto rounded-xl bg-row-hover p-4 font-mono text-xs leading-5">{JSON.stringify(doc.document, null, 2)}</pre>
        </FormDialog>
      )}
    </>
  );
}

type SettingsDoc = NonNullable<SyncCoursesResponse["settings_documents"]>[number];

/** The account's read, starred and hidden bulletins; synced to every device, unlike the subscriptions above. */
function BulletinStates({ courses }: { courses: SyncCoursesResponse }) {
  const counts = courses.bulletin_state_counts;
  const marked = courses.bulletin_states ?? [];
  if (!counts) return null;
  return (
    <div className="flex flex-col gap-2.5 border-t border-divider px-6 py-4 phone:px-4">
      <div className="flex flex-wrap gap-x-4 gap-y-1 text-[13px] text-ink-2 tabular-nums">
        <span>{counts.read} read</span>
        <span>{counts.starred} starred</span>
        <span>{counts.hidden} hidden</span>
      </div>
      {marked.length > 0 && (
        <ul className="flex max-h-64 flex-col overflow-y-auto rounded-xl border border-line">
          {marked.map((b) => (
            <li key={b.bulletin_id} className="flex min-w-0 items-center gap-2 border-b border-divider px-3.5 py-2 last:border-b-0">
              <span title={b.title} className="min-w-0 flex-1 truncate text-[13px]">
                {b.title}
              </span>
              {b.is_starred && <Pill tone="warning">Starred</Pill>}
              {b.is_hidden && <Pill>Hidden</Pill>}
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}

const modeLabel = (mode: string) => (mode === "any" ? "Any tag" : mode === "all" ? "All tags" : mode);

// ── Push ───────────────────────────────────────────────────────────────

const providerLabel = (p: string) => (p === "apns" ? "APNs" : p === "fcm" ? "FCM" : p);

function PushTable({ device, deliveries }: { device: SyncDevice; deliveries: PushDeliveryRow[] }) {
  const columns: Column<PushDeliveryRow>[] = [
    { key: "job", label: "Job", hide: "phone", sortValue: (d) => d.push_job_id, className: "font-mono text-[13px]", cell: (d) => `#${d.push_job_id}` },
    { key: "what", label: "What", sortValue: pushLabel, filterValue: pushLabel, cell: (d) => <div className="max-w-[220px] truncate phone:max-w-[130px]">{pushLabel(d)}</div> },
    { key: "provider", label: "Provider", hide: "tablet", filterValue: (d) => providerLabel(d.provider), className: "text-[13px] text-ink-3", cell: (d) => providerLabel(d.provider) },
    {
      key: "status",
      label: "Status",
      sortValue: (d) => d.status,
      filterValue: (d) => statusText(d.status),
      cell: (d) => <StatusPill status={d.status} dot={false} />,
    },
    { key: "attempts", label: "Attempts", hide: "tablet", className: "font-mono text-[13px]", cell: (d) => `${d.attempts}/${d.max_attempts}` },
    { key: "sent", label: "Sent", sortValue: (d) => d.sent_at ?? d.created_at, className: "whitespace-nowrap text-[13px] text-ink-3", cell: (d) => (d.sent_at ? monthDayTime(d.sent_at) : "—") },
    {
      key: "error",
      label: "Error",
      hide: "phone",
      className: "max-w-[200px]",
      cell: (d) => {
        const error = d.failure_code ?? d.failure_message;
        return error ? <Pill tone="danger" title={d.failure_message ?? undefined}>{error}</Pill> : <span className="text-ink-3">—</span>;
      },
    },
  ];
  const table = useTable({ rows: deliveries, columns, initialSort: { key: "sent", dir: "desc" } });
  return (
    <DataTable
      table={table}
      rowKey={(d) => String(d.id)}
      minWidth={680}
      empty={takesPush(device) || deliveries.length ? "Nothing here" : "No push token on this device"}
    />
  );
}

const statusText = (s: string) => ({ sent: "Sent", failed: "Failed", skipped: "Skipped", pending: "Pending" })[s] ?? s;

// ── Queued ─────────────────────────────────────────────────────────────

type Kind = "all" | "la" | "rem" | "sys";

export function kindOf(j: QueuedJob): Kind | null {
  if (j.channel === "schedule") return "la";
  if (j.channel === "course" || j.channel === "assignment") return "rem";
  if (j.channel === "system") return "sys";
  return null;
}

function QueuedList({ jobs, deviceId, studentId }: { jobs: QueuedJob[]; deviceId: string; studentId: string }) {
  const [kind, setKind] = useState<Kind>("all");
  const [ending, setEnding] = useState<Set<number>>(new Set());
  const shown = jobs.filter((j) => kind === "all" || kindOf(j) === kind);
  const endNow = (id: number) => {
    setEnding((s) => new Set(s).add(id));
    post(`/api/moodle/tests/live-activity-end-now?student_id=${encodeURIComponent(studentId)}&job_id=${id}`).then(
      () => toast.success("Live Activity end sent now"),
      (e) => {
        setEnding((s) => {
          const next = new Set(s);
          next.delete(id);
          return next;
        });
        toast.error(errorText(e));
      },
    );
  };
  const kinds: [Kind, string][] = [["all", "All"], ["la", "Live Activity"], ["rem", "Reminders"], ["sys", "System"]];

  return (
    <div className="flex flex-col gap-3.5 px-6 pb-5 pt-3.5 phone:px-4">
      <Chips
        label="Show"
        isOn={(k) => k === kind}
        onPick={setKind}
        options={kinds.map(([k, label]) => ({
          value: k,
          label,
          count: k === "all" ? jobs.length : jobs.filter((j) => kindOf(j) === k).length,
        }))}
      />
      {shown.length === 0 ? (
        <div className="p-6 text-center font-semibold">Nothing queued for this device</div>
      ) : (
        <ol className="flex flex-col">
          {shown.map((j) => {
            const due = Date.parse(j.fire_at) <= Date.now();
            const ready = j.recipients.find((r) => r.device_id === deviceId)?.token_ready ?? false;
            const isEnd = j.channel === "schedule" && j.kind === "live_activity_end";
            const content = [j.title, j.body].filter(Boolean).join(" · ") || j.scenario;
            return (
              <li key={j.id} className="grid grid-cols-[92px_20px_minmax(0,1fr)] gap-x-3 phone:grid-cols-[72px_16px_minmax(0,1fr)] phone:gap-x-2">
                <div className="flex flex-col items-end pt-2.5 tabular-nums">
                  <span className="whitespace-nowrap font-semibold">{until(j.fire_at)}</span>
                  <span className="text-xs text-ink-3">{clock(j.fire_at)}</span>
                </div>
                <div aria-hidden="true" className="flex flex-col items-center">
                  <span className="h-3.5 w-0.5 bg-line" />
                  <span className={cn("size-3 rounded-full border-2 bg-surface", due ? "border-dot-warn" : "border-link")} />
                  <span className="w-0.5 flex-1 bg-line" />
                </div>
                <div className="my-1.5 flex flex-wrap items-start justify-between gap-x-4 gap-y-2 rounded-xl border border-line px-3.5 py-2.5">
                  <div className="flex min-w-0 flex-[1_1_220px] flex-col gap-0.5">
                    <span className="font-semibold [overflow-wrap:anywhere]">{pushLabel(j)}</span>
                    <span title={content} className="truncate text-[13px] text-ink-2">{content}</span>
                    {!ready && (
                      <span className="mt-1 inline-flex items-center gap-1.5 text-xs text-badge-warning-fg">
                        <TriangleAlert className="h-3.5 w-3.5" />
                        {j.channel === "schedule" && !isEnd ? "Won’t arrive: no push-to-start token" : "Won’t arrive: no push token"}
                      </span>
                    )}
                  </div>
                  <div className="flex items-center gap-2">
                    <Pill tone={j.status === "processing" ? "warning" : "info"}>{j.status === "processing" ? "Processing" : "Pending"}</Pill>
                    {isEnd && j.status === "pending" && (
                      <Button
                        variant="line"
                        className="h-8 rounded-lg px-3 text-[13px] font-normal"
                        disabled={ending.has(j.id)}
                        onClick={() => endNow(j.id)}
                      >
                        End now
                      </Button>
                    )}
                  </div>
                </div>
              </li>
            );
          })}
        </ol>
      )}
    </div>
  );
}

// ── Info ───────────────────────────────────────────────────────────────

function Info({ device }: { device: SyncDevice }) {
  const when = (iso: string | null) => (iso ? monthDayTime(iso) : "—");
  const rows: [string, string, boolean?, boolean?][] = [
    ["Client device ID", device.client_device_id, true, true],
    ["Internal ID", device.id, true, true],
    ["Platform", deviceNoun(device.platform)],
    ["Device class", device.device_class ?? "—"],
    ["Model", device.device_model ?? device.device_name ?? "—", true],
    ["Locale", device.locale ?? "—", true],
    ["App version", device.app_version ?? "—", true],
    ["OS version", device.os_version ? `${platformLabel(device.platform)} ${device.os_version}` : "—"],
    ["Last seen", ago(device.last_seen_at)],
    ["Last login", when(device.last_login_at)],
    ["Registered", when(device.created_at)],
  ];
  return (
    <dl className="grid grid-cols-[repeat(auto-fit,minmax(240px,1fr))] gap-x-6 px-6 pb-5 pt-2 phone:px-4">
      {rows.map(([k, v, mono, copy]) => (
        <div key={k} className="flex min-w-0 flex-col gap-0.5 border-b border-divider py-2.5">
          <dt className="text-xs text-ink-3">{k}</dt>
          <dd className="flex min-w-0 items-center gap-1.5">
            <span title={v} className={cn("min-w-0 truncate", mono && "font-mono text-[13px]")}>
              {v}
            </span>
            {copy && (
              <button
                type="button"
                aria-label={`Copy ${k.toLowerCase()}`}
                title="Copy"
                onClick={() => copyText(v, `Copied ${k.toLowerCase()}`)}
                className="flex size-7 shrink-0 items-center justify-center rounded-md text-ink-3 hover:bg-soft"
              >
                <Copy className="h-3.5 w-3.5" />
              </button>
            )}
          </dd>
        </div>
      ))}
    </dl>
  );
}

