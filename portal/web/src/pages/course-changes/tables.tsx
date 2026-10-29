// The Course changes page's tables: courses, one course's changes, and the term's.

import { ArrowRight, Pencil, Trash2 } from "lucide-react";
import { cn } from "@/lib/cn";
import { monthDay } from "@/lib/format";
import { Button } from "@/components/ui/button";
import { DataTable, useTable, type Column } from "@/components/data-table";
import { Pill } from "@/components/page";
import {
  dateText,
  detailOf,
  firstDay,
  KIND,
  KIND_LABELS,
  finalDay,
  meetsText,
  rangeSub,
  type Change,
  type Course,
} from "./model";

const CHANGE_FILTERS = ["With changes", "None"];

export function courseColumns(selected: string | undefined, onPick: (no: string) => void): Column<Course>[] {
  return [
    {
      key: "course",
      label: "Course",
      sortValue: (c) => c.course_no,
      // Takes the room the count columns leave.
      className: "w-full py-2",
      cell: (c) => (
        <button
          type="button"
          aria-pressed={c.course_no === selected}
          onClick={() => onPick(c.course_no)}
          className="flex min-h-10 w-full flex-col items-start gap-0.5 text-left"
        >
          <span className="flex flex-wrap items-baseline gap-x-2">
            <span className="font-mono text-xs text-ink-3">{c.course_no}</span>
            <span className="font-medium">{c.name}</span>
          </span>
          <span className="text-xs text-ink-3">{meetsText(c)}</span>
        </button>
      ),
    },
    {
      key: "dept",
      label: "Dept",
      hide: "phone",
      sortValue: (c) => c.course_no.slice(0, 2),
      filterValue: (c) => c.course_no.slice(0, 2),
      className: "py-2 font-mono text-xs text-ink-2",
      cell: (c) => c.course_no.slice(0, 2),
    },
    {
      key: "users",
      label: "Users",
      align: "right",
      hide: "phone",
      sortValue: (c) => c.students,
      className: "py-2 tabular-nums",
      cell: (c) => c.students,
    },
    {
      key: "changes",
      label: "Changes",
      align: "right",
      sortValue: (c) => c.changes,
      filterValue: (c) => (c.changes > 0 ? "With changes" : "None"),
      filterOptions: CHANGE_FILTERS,
      className: "py-2",
      cell: (c) =>
        c.changes > 0 ? (
          <span className="inline-flex min-w-[22px] justify-center rounded-full bg-soft px-[7px] py-px text-xs font-semibold tabular-nums">
            {c.changes}
          </span>
        ) : (
          <span className="text-dot-off">–</span>
        ),
    },
  ];
}

function DateCell({ change, course }: { change: Change; course: Course | undefined }) {
  return (
    <div className="flex flex-col items-start">
      <span className="font-medium">{dateText(change)}</span>
      {change.mode === "range" && <span className="text-xs text-ink-3">{rangeSub(change, course)}</span>}
      {change.stale && (
        <Pill tone="warning" className="mt-1">
          No class day
        </Pill>
      )}
    </div>
  );
}

function DetailCell({ change, course }: { change: Change; course: Course | undefined }) {
  const d = detailOf(change, course);
  const chip = "rounded-md px-2 py-0.5 text-[13px]";
  const mono = change.kind === "room" ? "font-mono" : "";
  return (
    <div className="flex flex-wrap items-center gap-1.5">
      {change.kind === "note" ? (
        <span className="rounded-full bg-badge-violet px-2 py-0.5 text-[13px] font-semibold text-badge-violet-fg">{d.note}</span>
      ) : (
        <>
          {d.old && (
            <>
              <span className={cn(chip, mono, "bg-ground text-ink-3 line-through")}>{d.old}</span>
              <ArrowRight className="h-3.5 w-3.5 shrink-0 text-ink-3" />
            </>
          )}
          <span className={cn(chip, mono, "font-semibold", KIND[change.kind].soft, KIND[change.kind].text)}>
            {d.next}
          </span>
        </>
      )}
      {d.periods && <span className="text-xs text-ink-3">{d.periods}</span>}
    </div>
  );
}

export function courseChangeColumns(
  course: Course | undefined,
  today: string,
  onEdit: (c: Change) => void,
  onDelete: (c: Change) => void,
): Column<Change>[] {
  return [
    {
      key: "date",
      label: "Date",
      sortValue: firstDay,
      className: "whitespace-nowrap py-2.5",
      cell: (c) => <DateCell change={c} course={course} />,
    },
    {
      key: "kind",
      label: "Change",
      sortValue: (c) => KIND[c.kind].label,
      filterValue: (c) => KIND[c.kind].label,
      filterOptions: KIND_LABELS,
      className: "py-2.5",
      cell: (c) => <Pill tone={KIND[c.kind].tone}>{KIND[c.kind].label}</Pill>,
    },
    {
      key: "detail",
      label: "Detail",
      hide: "phone",
      className: "py-2.5",
      cell: (c) => <DetailCell change={c} course={course} />,
    },
    {
      key: "added",
      label: "Added",
      hide: "phone",
      sortValue: (c) => c.created_at,
      className: "whitespace-nowrap py-2.5 text-[13px] tabular-nums text-ink-3",
      cell: (c) => monthDay(c.created_at),
    },
    {
      key: "actions",
      label: "",
      className: "whitespace-nowrap py-1.5 text-right last:pr-4",
      cell: (c) => {
        const remove = (
          <Button variant="quiet" size="icon" aria-label="Delete" title="Delete" className="rounded-lg text-ink-2" onClick={() => onDelete(c)}>
            <Trash2 />
          </Button>
        );
        if (c.stale) return <div className="inline-flex">{remove}</div>;
        if (finalDay(c) < today) return <span className="text-xs text-ink-3">Past</span>;
        return (
          <div className="inline-flex gap-0.5">
            <Button variant="quiet" size="icon" aria-label="Edit" title="Edit" className="rounded-lg text-ink-2" onClick={() => onEdit(c)}>
              <Pencil />
            </Button>
            {remove}
          </div>
        );
      },
    },
  ];
}

export function AllChanges({
  rows,
  scope,
  byNo,
  onCourse,
  onDelete,
}: {
  rows: Change[];
  scope: "upcoming" | "past";
  byNo: ReadonlyMap<string, Course>;
  onCourse: (no: string) => void;
  onDelete: (c: Change) => void;
}) {
  const table = useTable({
    rows,
    perPage: 6,
    initialSort: { key: "date", dir: scope === "upcoming" ? "asc" : "desc" },
    columns: [
      {
        key: "date",
        label: "Date",
        sortValue: firstDay,
        className: "whitespace-nowrap py-2.5",
        cell: (c) => <DateCell change={c} course={byNo.get(c.course_no)} />,
      },
      {
        key: "course",
        label: "Course",
        sortValue: (c) => c.course_no,
        filterValue: (c) => c.course_no.slice(0, 2),
        className: "py-2.5",
        cell: (c) => (
          <button
            type="button"
            onClick={() => onCourse(c.course_no)}
            disabled={!byNo.has(c.course_no)}
            className="flex min-h-9 flex-wrap content-center items-baseline gap-x-2 text-left"
          >
            <span className="font-mono text-xs text-ink-3">{c.course_no}</span>
            <span className="font-medium">{byNo.get(c.course_no)?.name}</span>
          </button>
        ),
      },
      {
        key: "kind",
        label: "Change",
        sortValue: (c) => KIND[c.kind].label,
        filterValue: (c) => KIND[c.kind].label,
        filterOptions: KIND_LABELS,
        className: "py-2.5",
        cell: (c) => <Pill tone={KIND[c.kind].tone}>{KIND[c.kind].label}</Pill>,
      },
      {
        key: "detail",
        label: "Detail",
        hide: "phone",
        className: "py-2.5",
        cell: (c) => <DetailCell change={c} course={byNo.get(c.course_no)} />,
      },
      {
        key: "added",
        label: "Added",
        hide: "tablet",
        sortValue: (c) => c.created_at,
        className: "whitespace-nowrap py-2.5 text-[13px] tabular-nums text-ink-3",
        cell: (c) => monthDay(c.created_at),
      },
      {
        key: "actions",
        label: "",
        className: "whitespace-nowrap py-1.5 text-right last:pr-4",
        // A course nobody takes any more has no table of its own to delete from.
        cell: (c) =>
          !byNo.has(c.course_no) && (
            <Button variant="quiet" size="icon" aria-label="Delete" title="Delete" className="rounded-lg text-ink-2" onClick={() => onDelete(c)}>
              <Trash2 />
            </Button>
          ),
      },
    ],
  });
  return <DataTable table={table} rowKey={(c) => String(c.id)} minWidth={760} empty="No changes match these filters" />;
}
