// The backend's copy of the account: its courses (with the deleted ones),
// assignments and overrides, and the sign-in expired notice to test.

import { useState, type ReactNode } from "react";
import { BellRing, Send, Server } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Skeleton } from "@/components/ui/skeleton";
import { DataTable, useTable, type Column } from "@/components/data-table";
import { Pill } from "@/components/page";
import { cn } from "@/lib/cn";
import { Segmented, TabBar } from "./controls";
import { ago, deviceNoun } from "./format";
import { takesPush } from "./Topology";
import type { SyncAssignment, SyncCourse, SyncCoursesResponse, SyncDevice, SyncEventsResponse, SyncOverride } from "./types";

export type Lang = "zh" | "en";

type CourseRow = {
  key: string;
  code: string;
  name: string;
  custom: string | null;
  term: string;
  deleted: boolean;
  color: { kind: "Default" | "Preset" | "Custom"; label: string; swatches: string[] };
  device: string | null;
};

/** A term column for rows synced under every term; a Moodle mirror row's empty term reads as a dash. */
export function termColumn<R>(termOf: (row: R) => string | null): Column<R> {
  return {
    key: "term",
    label: "Term",
    hide: "phone",
    sortValue: (r) => termOf(r) ?? "",
    filterValue: (r) => termOf(r) || "—",
    className: "font-mono text-[13px]",
    cell: (r) => termOf(r) || "—",
  };
}

export const deviceName = (devices: SyncDevice[], id: string | null) => {
  const d = id ? devices.find((x) => x.id === id) : undefined;
  return d ? deviceNoun(d.platform) : null;
};

export function colorOf(c: SyncCourse, data: SyncCoursesResponse): CourseRow["color"] {
  const preset = c.color_hex
    ? data.palette_light.findIndex((p) => p.toLowerCase() === c.color_hex!.toLowerCase())
    : -1;
  if (preset >= 0)
    return { kind: "Preset", label: `Preset ${preset + 1}`, swatches: [data.palette_light[preset], data.palette_dark[preset]] };
  if (c.color_hex) return { kind: "Custom", label: c.color_hex, swatches: [c.color_hex] };
  return { kind: "Default", label: "Default", swatches: [c.default_color_light, c.default_color_dark] };
}

export const nameIn = (c: SyncCourse, lang: Lang) =>
  lang === "en" ? c.course_name_en || c.course_name : c.course_name;

export const customName = (c: SyncCourse, lang: Lang) =>
  c.custom_names?.[lang] ?? c.custom_names?.[lang === "zh" ? "en" : "zh"] ?? null;

export function Swatches({ colors }: { colors: string[] }) {
  return colors.map((c, i) => (
    <span
      key={i}
      aria-hidden="true"
      className="size-3.5 shrink-0 rounded shadow-[inset_0_0_0_1px_rgba(20,22,26,0.12)]"
      style={{ background: c }}
    />
  ));
}

const muted = (v: string | null) => (v ? v : <span className="text-ink-3">—</span>);

/** Submitted on Moodle, or the student's own mark, as the assignment row shows it. */
export function assignmentStatus(a: SyncAssignment, overrides: SyncOverride[]): string {
  const mark = overrides.find((o) => o.moodle_assignment_id === a.moodle_assignment_id)?.local_status;
  if (mark === "locally_completed") return "Done locally";
  if (mark === "ignored") return "Ignored";
  return a.provider_is_submitted ? "Submitted" : "Not submitted";
}

const ASSIGNMENT_TONE = {
  "Not submitted": "neutral",
  Submitted: "success",
  "Done locally": "info",
  Ignored: "neutral",
} as const;

/** "Nov 3, 23:59", and whether it is due within three days and still open. */
function due(a: SyncAssignment, status: string): [string, boolean] {
  if (!a.due_at) return ["—", false];
  const d = new Date(a.due_at);
  const text = `${d.toLocaleDateString("en-US", { month: "short", day: "numeric" })}, ${d.toLocaleTimeString("en-GB", { hour: "2-digit", minute: "2-digit" })}`;
  const left = d.getTime() - Date.now();
  return [text, status === "Not submitted" && left >= 0 && left <= 3 * 86_400_000];
}

export function assignmentColumns(overrides: SyncOverride[]): Column<SyncAssignment>[] {
  const status = (a: SyncAssignment) => assignmentStatus(a, overrides);
  return [
    { key: "title", label: "Title", sortValue: (a) => a.title, cell: (a) => <div title={a.title} className="max-w-[280px] truncate">{a.title}</div> },
    {
      key: "course",
      label: "Course",
      hide: "phone",
      sortValue: (a) => a.client_course_no || a.course_no || "",
      filterValue: (a) => a.client_course_no || a.course_no || null,
      className: "whitespace-nowrap font-mono text-[13px]",
      cell: (a) => a.client_course_no || a.course_no || "—",
    },
    termColumn((a) => a.semester),
    {
      key: "due",
      label: "Due",
      sortValue: (a) => (a.due_at ? Date.parse(a.due_at) : 0),
      className: "whitespace-nowrap",
      cell: (a) => {
        const [text, soon] = due(a, status(a));
        return soon ? <Pill tone="orange">{text} · soon</Pill> : text;
      },
    },
    {
      key: "status",
      label: "Status",
      sortValue: status,
      filterValue: status,
      filterOptions: ["Not submitted", "Submitted", "Done locally", "Ignored"],
      cell: (a) => <Pill tone={ASSIGNMENT_TONE[status(a) as keyof typeof ASSIGNMENT_TONE]}>{status(a)}</Pill>,
    },
  ];
}

type Tab = "courses" | "assignments" | "overrides" | "tests";

export function BackendPanel({
  events,
  courses,
  coursesError,
  devices,
  onReauth,
}: {
  events: SyncEventsResponse;
  courses?: SyncCoursesResponse;
  /** The alert for a failed course load; the courses and assignments tabs show it. */
  coursesError?: ReactNode;
  devices: SyncDevice[];
  onReauth: () => void;
}) {
  const [tab, setTab] = useState<Tab>("courses");
  const [lang, setLang] = useState<Lang>("zh");
  const t = events.topology;
  const overrides = events.overrides ?? [];
  const allDevices = events.devices ?? [];

  const courseRows: CourseRow[] = courses
    ? [
        ...courses.courses.map((c) => ({
          key: `c${c.id}`,
          code: c.client_course_no || c.course_no,
          name: nameIn(c, lang),
          custom: customName(c, lang),
          term: c.semester,
          deleted: false,
          color: colorOf(c, courses),
          device: deviceName(allDevices, c.updated_by_device_id),
        })),
        ...(courses.tombstones ?? []).map((tomb) => {
          const known = courses.courses.find((c) => c.course_no === tomb.course_no);
          return {
            key: `t${tomb.semester}${tomb.course_key}`,
            code: tomb.course_no ?? tomb.course_key,
            name: known ? nameIn(known, lang) : "",
            custom: null,
            term: tomb.semester,
            deleted: true,
            color: { kind: "Default" as const, label: "—", swatches: [] },
            device: deviceName(allDevices, tomb.deleted_by_device_id),
          };
        }),
      ]
    : [];

  const courseTable = useTable<CourseRow>({
    rows: courseRows,
    initialSort: { key: "code", dir: "asc" },
    columns: [
      {
        key: "color",
        label: "Color",
        hide: "phone",
        filterValue: (r) => (r.deleted ? null : r.color.kind),
        filterOptions: ["Default", "Preset", "Custom"],
        cell: (r) => (
          <div className="flex items-center gap-1.5">
            <Swatches colors={r.color.swatches} />
            <span className={cn("whitespace-nowrap text-[13px]", r.color.kind === "Custom" ? "font-mono" : "text-ink-3")}>
              {r.color.label}
            </span>
          </div>
        ),
      },
      { key: "code", label: "Code", sortValue: (r) => r.code, className: "whitespace-nowrap font-mono text-[13px]", cell: (r) => r.code },
      {
        key: "name",
        label: "Name",
        sortValue: (r) => r.name,
        cell: (r) => (
          <div title={r.name} className={cn("max-w-[220px] truncate phone:max-w-[130px]", r.deleted && "text-ink-3 line-through")}>
            {r.name || "—"}
          </div>
        ),
      },
      { key: "custom", label: "Custom name", hide: "tablet", cell: (r) => <div className="max-w-[200px] truncate">{muted(r.custom)}</div> },
      {
        key: "term",
        label: "Term",
        hide: "phone",
        sortValue: (r) => r.term,
        filterValue: (r) => r.term,
        className: "font-mono text-[13px]",
        cell: (r) => r.term,
      },
      {
        key: "state",
        label: "State",
        filterValue: (r) => (r.deleted ? "Deleted" : "Active"),
        filterOptions: ["Active", "Deleted"],
        cell: (r) => (r.deleted ? <Pill tone="orange">Deleted</Pill> : <Pill tone="success">Active</Pill>),
      },
      {
        key: "device",
        label: "Updated by",
        hide: "tablet",
        sortValue: (r) => r.device ?? "",
        filterValue: (r) => r.device,
        className: "whitespace-nowrap text-[13px] text-ink-3",
        cell: (r) => r.device ?? "—",
      },
    ],
  });

  const assignmentTable = useTable({
    rows: courses?.assignments ?? [],
    columns: assignmentColumns(overrides),
    initialSort: { key: "due", dir: "desc" },
  });

  const overrideStatus = (o: SyncOverride) =>
    o.local_status === "locally_completed" ? "Done locally" : o.local_status === "ignored" ? "Ignored" : o.local_status;
  const overrideTable = useTable<SyncOverride>({
    rows: overrides,
    initialSort: { key: "updated", dir: "desc" },
    columns: [
      { key: "mid", label: "Moodle ID", hide: "phone", sortValue: (o) => o.moodle_assignment_id, className: "font-mono text-[13px]", cell: (o) => o.moodle_assignment_id },
      { key: "title", label: "Title", sortValue: (o) => o.title ?? "", cell: (o) => <div className="max-w-[300px] truncate">{o.title ?? "—"}</div> },
      {
        key: "status",
        label: "Status",
        sortValue: overrideStatus,
        filterValue: overrideStatus,
        cell: (o) => <Pill tone={o.local_status === "locally_completed" ? "info" : "neutral"}>{overrideStatus(o)}</Pill>,
      },
      { key: "updated", label: "Updated", sortValue: (o) => Date.parse(o.updated_at), className: "whitespace-nowrap text-[13px] text-ink-3", cell: (o) => ago(o.updated_at) },
    ],
  });

  const loading = coursesError ? null : <Skeleton className="m-6 h-40" />;
  return (
    <section aria-labelledby="be-title" className="min-w-0 rounded-[14px] border border-line bg-surface">
      <div className="flex flex-col gap-3 border-b border-line px-6 pt-4 phone:px-4">
        <div className="flex flex-wrap items-center gap-2.5">
          <span aria-hidden="true" className="flex size-8 items-center justify-center rounded-[9px] bg-badge-info text-badge-info-fg">
            <Server className="h-4 w-4" />
          </span>
          <h2 id="be-title" className="text-[15px] font-semibold">
            Backend
          </h2>
          {t && (
            <>
              <Pill className="font-mono font-medium">rev {t.revision}</Pill>
              <Pill className="font-medium">{t.course_count} courses</Pill>
              {t.tombstone_count > 0 && (
                <Pill tone="orange" className="font-medium">
                  {t.tombstone_count} deleted
                </Pill>
              )}
              {t.courses_reset_at && (
                <span className="ml-auto whitespace-nowrap text-xs text-ink-3">Courses reset {ago(t.courses_reset_at)}</span>
              )}
            </>
          )}
        </div>
        <TabBar
          label="Backend data"
          value={tab}
          onChange={setTab}
          className="overflow-x-auto [scrollbar-width:none]"
          tabs={[
            { value: "courses", label: "Courses", count: courseRows.length },
            { value: "assignments", label: "Assignments", count: courses?.assignments?.length ?? 0 },
            { value: "overrides", label: "Overrides", count: overrides.length },
            { value: "tests", label: "Tests" },
          ]}
        />
      </div>
      {coursesError && (tab === "courses" || tab === "assignments") && <div className="px-6 pt-4 phone:px-4">{coursesError}</div>}
      {tab === "courses" && (
        <>
          <div className="flex justify-end border-b border-divider px-6 py-2.5 phone:px-4">
            <Segmented
              label="Course name language"
              value={lang}
              onChange={setLang}
              options={[
                { value: "zh", label: "中文" },
                { value: "en", label: "EN" },
              ]}
              itemClassName="min-h-7 text-xs"
            />
          </div>
          {courses ? (
            <DataTable table={courseTable} rowKey={(r) => r.key} minWidth={760} empty="Nothing matches these filters" />
          ) : (
            loading
          )}
        </>
      )}
      {tab === "assignments" &&
        (courses ? (
          <DataTable table={assignmentTable} rowKey={(a) => String(a.id)} minWidth={640} empty="No assignments" />
        ) : (
          loading
        ))}
      {tab === "overrides" && (
        <DataTable table={overrideTable} rowKey={(o) => String(o.moodle_assignment_id)} minWidth={560} empty="No overrides" />
      )}
      {tab === "tests" && (
        <div className="px-6 pb-6 pt-5 phone:px-4">
          <div className="flex flex-wrap items-center justify-between gap-4 rounded-xl border border-line px-[18px] py-4">
            <div className="flex min-w-0 items-center gap-3">
              <span aria-hidden="true" className="flex size-9 shrink-0 items-center justify-center rounded-[10px] bg-badge-warning text-badge-warning-fg">
                <BellRing className="h-4 w-4" />
              </span>
              <div className="flex flex-col gap-0.5">
                <span className="font-semibold">Moodle sign-in expired notice</span>
                <span className="flex flex-wrap gap-1.5 text-xs">
                  {devices.map((d) => (
                    <span
                      key={d.id}
                      className={cn(
                        "rounded-full px-[7px] py-px",
                        takesPush(d) ? "bg-badge-success text-badge-success-fg" : "bg-soft text-ink-2 line-through",
                      )}
                    >
                      {deviceNoun(d.platform)}
                    </span>
                  ))}
                </span>
              </div>
            </div>
            <Button variant="line" size="form" onClick={onReauth}>
              <Send />
              Send test notice
            </Button>
          </div>
        </div>
      )}
    </section>
  );
}
