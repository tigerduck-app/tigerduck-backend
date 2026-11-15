// The term's pending items, one row each, with their actions.

import { Newspaper } from "lucide-react";
import { monthDayTime } from "@/lib/format";
import { DataTable, useTable } from "@/components/data-table";
import { EmptyState } from "@/components/page";
import type { Change, Course } from "../course-changes/model";
import { ChangeDetail, CheckPills, ItemActions, KindPills, type ItemHandlers } from "./items";
import type { Item, ReviewItem } from "./model";

export function ReviewTable({
  items,
  term,
  courses,
  changes,
  selected,
  onSelect,
  canEdit,
  handlers,
}: {
  items: ReviewItem[];
  term: string;
  courses: ReadonlyMap<string, Course>;
  changes: ReadonlyMap<number, Change>;
  selected: string | null;
  onSelect: (announcementId: string) => void;
  canEdit: (item: Item) => boolean;
  handlers: ItemHandlers;
}) {
  const existing = (i: Item) => (i.existing_change_id === null ? undefined : changes.get(i.existing_change_id));
  const table = useTable({
    rows: items,
    perPage: 10,
    columns: [
      {
        key: "course",
        label: "Course",
        className: "max-w-[300px] py-2.5 phone:max-w-none",
        cell: (i) => (
          <div className="flex min-w-0 flex-col items-start gap-2">
            <button
              type="button"
              onClick={() => onSelect(i.announcement_id)}
              className="flex w-full min-w-0 flex-col items-start gap-0.5 text-left"
            >
              <span className="flex max-w-full flex-wrap items-baseline gap-x-2">
                <span className="font-mono text-xs text-ink-3">{i.course_no}</span>
                <span className="font-medium">{courses.get(i.course_no)?.name}</span>
              </span>
              <span className="max-w-full truncate text-xs text-ink-3 phone:line-clamp-2 phone:whitespace-normal">
                {i.subject}
              </span>
            </button>
            {/* A phone has no room for the Change and Detail columns. */}
            <div className="hidden flex-col items-start gap-1.5 phone:flex">
              <KindPills item={i} term={term} />
              <ChangeDetail item={i} term={term} course={courses.get(i.course_no)} existing={existing(i)} />
            </div>
          </div>
        ),
      },
      {
        key: "change",
        label: "Change",
        hide: "phone",
        className: "py-2.5",
        cell: (i) => <KindPills item={i} term={term} />,
      },
      {
        key: "detail",
        label: "Detail",
        hide: "phone",
        className: "py-2.5",
        cell: (i) => (
          <ChangeDetail item={i} term={term} course={courses.get(i.course_no)} existing={existing(i)} />
        ),
      },
      {
        key: "checks",
        label: "Checks",
        hide: "phone",
        className: "py-2.5",
        cell: (i) => <CheckPills item={i} />,
      },
      {
        key: "posted",
        label: "Posted",
        hide: "phone",
        className: "whitespace-nowrap py-2.5 text-[13px] tabular-nums text-ink-2",
        cell: (i) => monthDayTime(i.posted_at),
      },
      {
        key: "actions",
        label: "",
        className: "whitespace-nowrap py-2 text-right phone:align-top",
        cell: (i) => <ItemActions item={i} {...handlers} onEdit={canEdit(i) ? handlers.onEdit : undefined} />,
      },
    ],
  });
  if (items.length === 0) return <EmptyState icon={Newspaper} title="No pending reviews" />;
  return (
    <DataTable
      table={table}
      rowKey={(i) => String(i.id)}
      minWidth={860}
      empty=""
      rowClassName={(i) =>
        i.announcement_id === selected && "bg-selected shadow-[inset_3px_0_0_var(--color-link)] hover:bg-selected"
      }
    />
  );
}
