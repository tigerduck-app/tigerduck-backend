// Course changes: the school's cancelled classes, room and time changes and
// notes per course and term, which the apps show in place of the regular class.

import { useRef, useState } from "react";
import { Link } from "react-router-dom";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { toast } from "sonner";
import { CalendarClock, ChevronDown, MapPin, Plus, Search, Users } from "lucide-react";
import { api } from "@/lib/api";
import { cn } from "@/lib/cn";
import {
  CALENDAR_KEY,
  daysBetween,
  fetchCalendar,
  holidayNames,
  mondayOf,
  periodRuns,
  periodSpan,
  rangeText,
  termLabel,
  todayIso,
  WEEKDAYS,
  type Term,
} from "@/lib/academic";
import { Button } from "@/components/ui/button";
import { Skeleton } from "@/components/ui/skeleton";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";
import { DataTable, useTable } from "@/components/data-table";
import { EmptyState, ErrorAlert, PageHeading, Panel } from "@/components/page";
import { asMessage } from "./lists/format";
import { ChangeDialog, type ChangeBody } from "./course-changes/ChangeDialog";
import {
  changeError,
  lastDay,
  reach,
  roomOf,
  type Change,
  type Course,
} from "./course-changes/model";
import { DeleteDialog } from "./course-changes/DeleteDialog";
import { AllChanges, courseChangeColumns, courseColumns } from "./course-changes/tables";
import { TermStrip } from "./course-changes/TermStrip";

const CHANGES_KEY = "course-changes";

/** The term today falls in, else the next to start, else the newest. */
function defaultTerm(terms: readonly Term[], today: string): Term | undefined {
  const upcoming = terms.filter((t) => t.end_date >= today).sort((a, b) => a.start_date.localeCompare(b.start_date));
  return upcoming[0] ?? terms[0];
}

function termMeta(term: Term, today: string): string {
  const total = Math.ceil((daysBetween(mondayOf(term.start_date), term.end_date) + 1) / 7);
  const range = rangeText(term.start_date, term.end_date);
  if (today < term.start_date || today > term.end_date) return `${range} · ${total} weeks`;
  return `${range} · Week ${Math.floor(daysBetween(mondayOf(term.start_date), today) / 7) + 1} of ${total}`;
}


export function CourseChangesPage() {
  const qc = useQueryClient();
  const today = todayIso();
  const [picked, setPicked] = useState<string | null>(null);
  const [selectedNo, setSelectedNo] = useState<string | null>(null);
  const [query, setQuery] = useState("");
  const [scope, setScope] = useState<"upcoming" | "past">("upcoming");
  // `key` tells dialogs apart, so a late answer never closes a newer one.
  const [dialog, setDialog] = useState<{ key: number; change: Change | null; day: string | null } | null>(null);
  const [deleting, setDeleting] = useState<{ key: number; change: Change } | null>(null);
  const opened = useRef(0);
  const dialogKey = useRef(0);
  dialogKey.current = dialog?.key ?? 0;

  const calendarQ = useQuery({ queryKey: CALENDAR_KEY, queryFn: fetchCalendar });
  const terms = calendarQ.data?.terms ?? [];
  const term = terms.find((t) => t.code === picked) ?? defaultTerm(terms, today);
  const code = term?.code ?? "";
  const path = `/api/course-changes/terms/${encodeURIComponent(code)}`;
  const coursesQ = useQuery({
    queryKey: [CHANGES_KEY, code, "courses"],
    queryFn: () => api<{ courses: Course[] }>(`${path}/courses`),
    enabled: code !== "",
  });
  const changesQ = useQuery({
    queryKey: [CHANGES_KEY, code, "changes"],
    queryFn: () => api<{ changes: Change[] }>(`${path}/changes`),
    enabled: code !== "",
  });
  const refresh = () => qc.invalidateQueries({ queryKey: [CHANGES_KEY, code] });

  const courses = coursesQ.data?.courses ?? [];
  const changes = changesQ.data?.changes ?? [];
  const byNo = new Map(courses.map((c) => [c.course_no, c]));
  const holidays = holidayNames(calendarQ.data?.holidays ?? []);
  const course = byNo.get(selectedNo ?? "") ?? courses[0];
  const mine = course ? changes.filter((c) => c.course_no === course.course_no) : [];

  const select = (no: string) => {
    setSelectedNo(no);
    // On one column the course sits under the list; bring it into view.
    const heading = document.getElementById("course-h");
    const top = heading?.getBoundingClientRect().top ?? 0;
    if (heading && (top < 0 || top > window.innerHeight)) heading.scrollIntoView({ block: "start" });
  };
  const openAdd = (day: string | null) => setDialog({ key: ++opened.current, change: null, day });
  const openEdit = (change: Change) => setDialog({ key: ++opened.current, change, day: null });
  const askDelete = (change: Change) => setDeleting({ key: ++opened.current, change });

  const saveMut = useMutation({
    mutationFn: ({ change, term, body }: { key: number; change: Change | null; term: string; body: ChangeBody }) =>
      change
        ? api<Change>(`/api/course-changes/changes/${change.id}`, { method: "PUT", json: body })
        : api<Change>(`/api/course-changes/terms/${encodeURIComponent(term)}/changes`, { method: "POST", json: body }),
    onSuccess: (_, { key, change }) => {
      toast.success(change ? "Change updated" : "Change added");
      setDialog((d) => (d?.key === key ? null : d));
      refresh();
    },
    // The open dialog shows its own error; one closed meanwhile gets a toast.
    onError: (e, { key, body }) => {
      if (dialogKey.current !== key) toast.error(changeError(e, body.kind));
    },
  });

  const deleteMut = useMutation({
    mutationFn: ({ change }: { key: number; change: Change }) =>
      api(`/api/course-changes/changes/${change.id}`, { method: "DELETE" }),
    onSuccess: (_, { key }) => {
      toast.success("Change deleted");
      setDeleting((d) => (d?.key === key ? null : d));
      refresh();
    },
  });

  const q = query.trim().toLowerCase();
  const courseTable = useTable({
    rows: courses,
    columns: courseColumns(course?.course_no, select),
    perPage: 12,
    search: q,
    match: (c, s) =>
      [c.course_no, c.name, c.name_en ?? "", ...c.instructors].some((v) => v.toLowerCase().includes(s)),
  });
  const mineTable = useTable({
    rows: mine,
    columns: courseChangeColumns(course, today, openEdit, askDelete),
    perPage: 5,
    initialSort: { key: "date", dir: "asc" },
  });
  const upcomingMine = mine.filter((c) => lastDay(c) >= today).length;
  const upcomingAll = changes.filter((c) => lastDay(c) >= today).length;

  if (calendarQ.isError) {
    return (
      <>
        <PageHeading group="Academic" title="Course changes" />
        <ErrorAlert className="items-center" onRetry={() => calendarQ.refetch()}>
          Failed to load the calendar: {asMessage(calendarQ.error)}
        </ErrorAlert>
      </>
    );
  }

  return (
    <>
      <PageHeading
        group="Academic"
        title="Course changes"
        meta={term && <span className="font-sans">{termMeta(term, today)}</span>}
        actions={term && <TermPicker terms={terms} term={term} onPick={(c) => setPicked(c)} />}
      />

      {calendarQ.isLoading && <Skeleton className="h-96" />}
      {calendarQ.data && !term && (
        <Panel>
          <EmptyState
            icon={CalendarClock}
            title="No terms"
            action={
              <Button asChild variant="ink" size="form">
                <Link to="/semester-dates">Add term</Link>
              </Button>
            }
          />
        </Panel>
      )}

      {term && (
        <div className="grid grid-cols-[minmax(0,5fr)_minmax(0,7fr)] items-stretch gap-6 tablet:grid-cols-1">
          <section
            aria-labelledby="courses-h"
            className="flex min-w-0 flex-col overflow-hidden rounded-[14px] border border-line bg-surface [&>:last-child]:mt-auto"
          >
            <div className="flex flex-col gap-3 border-b border-line px-4 pb-3 pt-4">
              <div className="flex items-baseline justify-between">
                <h2 id="courses-h" className="text-[15px] font-semibold">
                  Courses
                </h2>
                <span className="text-xs tabular-nums text-ink-3">
                  {courses.length} {courses.length === 1 ? "course" : "courses"}
                </span>
              </div>
              <label className="flex h-10 items-center gap-2 rounded-[9px] border border-field px-3 text-ink-3">
                <Search className="h-[15px] w-[15px] shrink-0" />
                <input
                  type="search"
                  aria-label="Search courses"
                  placeholder="Code, name or instructor"
                  value={query}
                  onChange={(e) => setQuery(e.target.value)}
                  className="min-w-0 flex-1 bg-transparent text-ink outline-none placeholder:text-ink-3"
                />
              </label>
            </div>
            {coursesQ.isLoading && <Skeleton className="m-4 h-64" />}
            {coursesQ.isError && (
              <ErrorAlert className="m-4 items-center" onRetry={() => coursesQ.refetch()}>
                Failed to load courses: {asMessage(coursesQ.error)}
              </ErrorAlert>
            )}
            {coursesQ.data && courses.length === 0 && <EmptyState icon={CalendarClock} title="No courses" />}
            {courses.length > 0 && (
              <DataTable
                table={courseTable}
                rowKey={(c) => c.course_no}
                minWidth={380}
                rowClassName={(c) => c.course_no === course?.course_no && "bg-selected shadow-[inset_3px_0_0_var(--color-link)] hover:bg-selected"}
                empty={
                  <div className="flex flex-col items-center gap-2.5 py-4">
                    <span className="text-[15px] font-semibold text-ink">No matching courses</span>
                    <Button variant="line" size="bar" className="font-normal" onClick={() => setQuery("")}>
                      Clear search
                    </Button>
                  </div>
                }
              />
            )}
          </section>

          <div className="flex min-w-0 flex-col gap-5">
            {course && (
              <section
                aria-labelledby="course-h"
                className="flex scroll-mt-4 flex-col gap-[18px] rounded-[14px] border border-line bg-surface px-6 py-5 phone:px-4"
              >
                <div className="flex flex-wrap items-start justify-between gap-3">
                  <div className="flex min-w-0 flex-col gap-1">
                    <span className="font-mono text-[13px] text-ink-3">{course.course_no}</span>
                    <h2 id="course-h" className="text-xl font-semibold">
                      {course.name} {course.name_en && <span className="font-normal text-ink-3">{course.name_en}</span>}
                    </h2>
                    <div className="flex flex-wrap items-center gap-x-3.5 gap-y-1.5 text-[13px] text-ink-2">
                      {course.instructors.length > 0 && <span>{course.instructors.join(", ")}</span>}
                      {course.credits !== null && (
                        <span>
                          {course.credits} {course.credits === 1 ? "credit" : "credits"}
                        </span>
                      )}
                      <span className="inline-flex items-center gap-1.5">
                        <Users className="h-[15px] w-[15px]" />
                        {reach(course)}
                      </span>
                    </div>
                  </div>
                  <Button variant="ink" size="form" className="pl-3" onClick={() => openAdd(null)}>
                    <Plus />
                    Add change
                  </Button>
                </div>
                <MeetChips course={course} />
                <TermStrip
                  term={term}
                  course={course}
                  holidays={holidays}
                  changes={mine}
                  today={today}
                  onAdd={openAdd}
                  onEdit={openEdit}
                />
              </section>
            )}

            {course && (
              <Panel
                title="Changes to this course"
                aside={
                  <span className="text-xs tabular-nums text-ink-3">
                    {upcomingMine} upcoming · {mine.length - upcomingMine} past
                  </span>
                }
              >
                {changesQ.isLoading && <Skeleton className="m-6 h-24" />}
                {changesQ.isError && (
                  <ErrorAlert className="m-6 items-center" onRetry={() => changesQ.refetch()}>
                    Failed to load changes: {asMessage(changesQ.error)}
                  </ErrorAlert>
                )}
                {changesQ.data && mine.length === 0 && (
                  <EmptyState
                    icon={CalendarClock}
                    title="No changes this term"
                    action={
                      <Button variant="ink" size="form" onClick={() => openAdd(null)}>
                        Add change
                      </Button>
                    }
                  />
                )}
                {mine.length > 0 && (
                  <DataTable table={mineTable} rowKey={(c) => String(c.id)} minWidth={560} empty="No changes match these filters" />
                )}
              </Panel>
            )}
          </div>
        </div>
      )}

      {term && changesQ.data && (
        <Panel
          title={`All changes in ${termLabel(term.code)}`}
          aside={
            <div role="radiogroup" aria-label="When" className="flex gap-1 rounded-[10px] bg-soft p-1">
              {(
                [
                  ["upcoming", `Upcoming · ${upcomingAll}`],
                  ["past", `Past · ${changes.length - upcomingAll}`],
                ] as const
              ).map(([v, label]) => (
                <button
                  key={v}
                  type="button"
                  role="radio"
                  aria-checked={scope === v}
                  onClick={() => setScope(v)}
                  className={cn(
                    "min-h-8 rounded-[7px] px-3 text-[13px]",
                    scope === v ? "bg-surface font-semibold text-ink shadow-[0_1px_2px_rgba(20,22,26,0.12)]" : "text-ink-2",
                  )}
                >
                  {label}
                </button>
              ))}
            </div>
          }
        >
          <AllChanges
            key={`${code}-${scope}`}
            rows={changes.filter((c) => (lastDay(c) >= today) === (scope === "upcoming"))}
            scope={scope}
            byNo={byNo}
            onCourse={select}
          />
        </Panel>
      )}

      {dialog && course && term && (
        <ChangeDialog
          key={dialog.key}
          course={byNo.get(dialog.change?.course_no ?? "") ?? course}
          term={term}
          holidays={holidays}
          today={today}
          existing={changes.filter(
            (c) => c.course_no === (dialog.change?.course_no ?? course.course_no) && c.id !== dialog.change?.id,
          )}
          change={dialog.change}
          initDay={dialog.day}
          pending={saveMut.isPending}
          error={
            saveMut.isError && saveMut.variables?.key === dialog.key
              ? changeError(saveMut.error, saveMut.variables.body.kind)
              : null
          }
          onClose={() => setDialog(null)}
          onSave={(body) => saveMut.mutate({ key: dialog.key, change: dialog.change, term: term.code, body })}
          onDelete={() => {
            const change = dialog.change;
            setDialog(null);
            if (change) askDelete(change);
          }}
        />
      )}

      {deleting && (
        <DeleteDialog
          change={deleting.change}
          course={byNo.get(deleting.change.course_no)}
          pending={deleteMut.isPending}
          error={deleteMut.isError && deleteMut.variables?.key === deleting.key ? asMessage(deleteMut.error) : null}
          onCancel={() => setDeleting(null)}
          onConfirm={() => deleteMut.mutate(deleting)}
        />
      )}
    </>
  );
}

function TermPicker({ terms, term, onPick }: { terms: readonly Term[]; term: Term; onPick: (code: string) => void }) {
  const recent = terms.slice(0, 3);
  const older = terms.slice(3);
  const olderPicked = older.some((t) => t.code === term.code);
  const segment = (on: boolean) =>
    cn(
      "flex min-h-9 items-center rounded-[7px] px-3.5",
      on ? "bg-surface font-semibold text-ink shadow-[0_1px_2px_rgba(20,22,26,0.12)]" : "text-ink-2 hover:text-ink",
    );
  return (
    <div role="group" aria-label="Term" className="flex flex-wrap gap-1 rounded-[10px] bg-soft p-1">
      {recent.map((t) => (
        <button
          key={t.code}
          type="button"
          aria-pressed={t.code === term.code}
          onClick={() => onPick(t.code)}
          className={segment(t.code === term.code)}
        >
          {termLabel(t.code)}
        </button>
      ))}
      {older.length > 0 && (
        <DropdownMenu>
          <DropdownMenuTrigger asChild>
            <button type="button" aria-pressed={olderPicked} className={cn(segment(olderPicked), "gap-1 pr-2.5")}>
              {olderPicked ? termLabel(term.code) : "Older"}
              <ChevronDown className="h-3.5 w-3.5" />
            </button>
          </DropdownMenuTrigger>
          <DropdownMenuContent align="end" className="rounded-xl border-line bg-surface p-1.5 text-ink">
            {older.map((t) => (
              <DropdownMenuItem key={t.code} onSelect={() => onPick(t.code)} className="min-h-[34px]">
                {termLabel(t.code)}
              </DropdownMenuItem>
            ))}
          </DropdownMenuContent>
        </DropdownMenu>
      )}
    </div>
  );
}

function MeetChips({ course }: { course: Course }) {
  return (
    <div role="list" aria-label="Meets" className="flex flex-wrap gap-2">
      {course.meetings.flatMap((m) =>
        periodRuns(m.periods).map((run) => {
          const room = roomOf(course, m.day, run);
          return (
            <span
              key={`${m.day}-${run[0]}`}
              role="listitem"
              className="inline-flex min-h-9 items-center gap-2.5 rounded-[10px] bg-ground pl-1 pr-3 text-[13px]"
            >
              <span className="flex h-7 min-w-10 items-center justify-center rounded-[7px] bg-chart-1 px-2 text-xs font-semibold text-white">
                {WEEKDAYS[m.day]}
              </span>
              <span className="font-mono text-xs tabular-nums">{periodSpan(run)?.join("–") ?? run.join(", ")}</span>
              {room && (
                <span className="inline-flex items-center gap-1 font-mono text-xs text-ink-2">
                  <MapPin className="h-3.5 w-3.5" />
                  {room}
                </span>
              )}
            </span>
          );
        }),
      )}
    </div>
  );
}
