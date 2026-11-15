// A page of the term's announcements: search, the extraction filters and
// the server's paging; the excerpt shows only while the text is revealed.

import { useQuery } from "@tanstack/react-query";
import { Newspaper, Search } from "lucide-react";
import { api } from "@/lib/api";
import { cn } from "@/lib/cn";
import { monthDayTime } from "@/lib/format";
import { DataTable, Pager, PiiToggle, useTable } from "@/components/data-table";
import { EmptyState, ErrorAlert, Pill } from "@/components/page";
import { Button } from "@/components/ui/button";
import { Skeleton } from "@/components/ui/skeleton";
import { asMessage } from "../lists/format";
import type { Course } from "../course-changes/model";
import { KEY, statusOf, type AnnouncementPage, type Filter, type Listed } from "./model";

const FILTERS: [Filter, string][] = [
  ["all", "All"],
  ["extracted", "Extracted"],
  ["failed", "Failed"],
  ["differs", "Differs"],
];

export type ListState = { query: string; filter: Filter; page: number };

export function AnnouncementList({
  term,
  state,
  onState,
  courses,
  selected,
  onSelect,
  showPii,
  onTogglePii,
}: {
  term: string;
  state: ListState;
  onState: (s: ListState) => void;
  courses: ReadonlyMap<string, Course>;
  selected: string | null;
  onSelect: (id: string) => void;
  showPii: boolean;
  onTogglePii: () => void;
}) {
  const q = state.query.trim();
  const listQ = useQuery({
    queryKey: [KEY, term, "announcements", q, state.filter, state.page],
    queryFn: () =>
      api<AnnouncementPage>(
        `/api/course-announcements/terms/${encodeURIComponent(term)}/announcements?${new URLSearchParams({
          ...(q ? { q } : {}),
          extraction: state.filter,
          page: String(state.page),
        })}`,
      ),
    placeholderData: (previous) => previous,
  });
  const data = listQ.data;
  const rows = data?.announcements ?? [];
  const total = data?.counts[state.filter] ?? 0;
  const table = useTable({
    rows,
    perPage: Infinity,
    columns: [
      {
        key: "announcement",
        label: "Announcement",
        className: "w-full max-w-0 py-2.5",
        cell: (a: Listed) => <ListedCell a={a} name={courses.get(a.course_codes[0])?.name} showPii={showPii} onSelect={onSelect} />,
      },
      {
        key: "extraction",
        label: "Extraction",
        className: "whitespace-nowrap py-2.5",
        cell: (a) => {
          const [label, tone] = statusOf(a);
          return <Pill tone={tone}>{label}</Pill>;
        },
      },
      {
        key: "posted",
        label: "Posted",
        hide: "phone",
        className: "whitespace-nowrap py-2.5 text-[13px] tabular-nums text-ink-2",
        cell: (a) => monthDayTime(a.posted_at),
      },
    ],
  });

  return (
    <>
      <div className="flex flex-wrap items-center gap-2 px-6 pb-3 pt-3.5 phone:px-4">
        <label className="flex h-9 w-[260px] min-w-0 items-center gap-2 rounded-[9px] border border-field bg-surface px-3 text-ink-3 phone:w-auto phone:flex-1">
          <Search className="h-[15px] w-[15px] shrink-0" />
          <input
            type="search"
            aria-label="Search announcements"
            placeholder="Code, name or subject"
            maxLength={200}
            value={state.query}
            onChange={(e) => onState({ ...state, query: e.target.value, page: 1 })}
            className="min-w-0 flex-1 bg-transparent text-ink outline-none placeholder:text-ink-3"
          />
        </label>
        <PiiToggle shown={showPii} onToggle={onTogglePii} what="announcement text" />
      </div>
      <div
        role="radiogroup"
        aria-label="Extraction"
        className="mx-6 mb-3 flex w-fit max-w-[calc(100%-48px)] flex-wrap gap-1 rounded-[10px] bg-soft p-1 phone:mx-4 phone:max-w-[calc(100%-32px)]"
      >
        {FILTERS.map(([value, label]) => (
          <button
            key={value}
            type="button"
            role="radio"
            aria-checked={state.filter === value}
            onClick={() => onState({ ...state, filter: value, page: 1 })}
            className={cn(
              "min-h-8 rounded-[7px] px-3 text-[13px]",
              state.filter === value
                ? "bg-surface font-semibold text-ink shadow-[0_1px_2px_rgba(20,22,26,0.12)]"
                : "text-ink-2",
            )}
          >
            {value === "all" || !data ? label : `${label} · ${data.counts[value].toLocaleString("en-US")}`}
          </button>
        ))}
      </div>
      {listQ.isLoading && <Skeleton className="m-6 h-64" />}
      {listQ.isError && (
        <ErrorAlert className="m-6 items-center" onRetry={() => listQ.refetch()}>
          Failed to load announcements: {asMessage(listQ.error)}
        </ErrorAlert>
      )}
      {data && data.counts.all === 0 && !q ? (
        <EmptyState icon={Newspaper} title="No announcements" />
      ) : (
        data && (
          <>
            <DataTable
              table={table}
              rowKey={(a) => a.id}
              minWidth={420}
              rowClassName={(a) =>
                a.id === selected && "bg-selected shadow-[inset_3px_0_0_var(--color-link)] hover:bg-selected"
              }
              empty={
                <div className="flex flex-col items-center gap-2.5 py-6">
                  <span className="text-[15px] font-semibold text-ink">No matching announcements</span>
                  {q && (
                    <Button variant="line" size="bar" className="font-normal" onClick={() => onState({ ...state, query: "", page: 1 })}>
                      Clear search
                    </Button>
                  )}
                </div>
              }
            />
            <Pager
              table={{
                page: state.page,
                pages: Math.max(1, Math.ceil(total / data.per_page)),
                perPage: data.per_page,
                total,
                rows,
                setPage: (page) => onState({ ...state, page }),
              }}
            />
          </>
        )
      )}
    </>
  );
}

function ListedCell({
  a,
  name,
  showPii,
  onSelect,
}: {
  a: Listed;
  name: string | undefined;
  showPii: boolean;
  onSelect: (id: string) => void;
}) {
  return (
    <button type="button" onClick={() => onSelect(a.id)} className="flex w-full min-w-0 flex-col items-start gap-[3px] text-left">
      <span className="flex w-full min-w-0 items-baseline gap-1.5 text-xs text-ink-3">
        <span className="shrink-0 font-mono">{a.course_codes.join(", ")}</span>
        {name && <span className="truncate">{name}</span>}
      </span>
      <span className="w-full truncate font-medium">{a.subject}</span>
      {a.labels.length > 0 && (
        <span className="flex flex-wrap gap-1">
          {a.labels.map((l) => (
            <span
              key={l.label}
              className="inline-flex items-center gap-[5px] rounded-full border border-line bg-ground px-2 py-px font-mono text-[11px] text-ink-2"
            >
              {l.label} <span className="font-medium text-ink-3">{l.score?.toFixed(2)}</span>
            </span>
          ))}
        </span>
      )}
      {showPii && a.excerpt && <span className="w-full truncate text-xs text-ink-3">{a.excerpt}</span>}
    </button>
  );
}
