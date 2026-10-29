// Semester dates: the term ranges and school holidays the apps read from
// /v3/calendar/semesters, signed in or not.

import { useRef, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { toast } from "sonner";
import { Calendar, ChevronLeft, ChevronRight, Pencil, Plus, Trash2, TriangleAlert, Users } from "lucide-react";
import { api } from "@/lib/api";
import { cn } from "@/lib/cn";
import {
  CALENDAR_KEY,
  daysBetween,
  fetchCalendar,
  rangeText,
  termLabel,
  todayIso,
  weeks,
  type Holiday,
  type Term,
} from "@/lib/academic";
import { Button } from "@/components/ui/button";
import { Skeleton } from "@/components/ui/skeleton";
import { DataTable, useTable, type Column } from "@/components/data-table";
import { EmptyState, ErrorAlert, FormDialog, PageHeading, Panel, Pill, type Tone } from "@/components/page";
import { asMessage } from "./lists/format";
import { SemesterDialog, type SemesterDraft, type SemesterSave } from "./semester-dates/SemesterDialog";
import { Timeline } from "./semester-dates/Timeline";

type Status = "Current" | "Upcoming" | "Past" | "No dates";
const STATUSES: Status[] = ["Current", "Upcoming", "Past", "No dates"];
const STATUS_TONE: Record<Status, Tone> = {
  Current: "info",
  Upcoming: "violet",
  Past: "neutral",
  "No dates": "warning",
};

type TermRow = { code: string; term: Term | null; status: Status };
type HolidayRow = Holiday & { during: string };
type Deleting = { kind: "term"; term: Term } | { kind: "holiday"; holiday: Holiday };

/** Academic year N runs from Aug 1 of N + 1911 to Jul 31 of the next year. */
function ayOf(iso: string): number {
  return Number(iso.slice(0, 4)) - 1911 - (Number(iso.slice(5, 7)) < 8 ? 1 : 0);
}

const iconButton = "size-[34px] rounded-lg text-ink-3";

export function SemesterDatesPage() {
  const qc = useQueryClient();
  const today = todayIso();
  const [ay, setAy] = useState(() => ayOf(today));
  // `key` tells dialogs apart, so a late answer never closes a newer one.
  const [dialog, setDialog] = useState<{ key: number; draft: SemesterDraft; editing: boolean } | null>(null);
  const [deleting, setDeleting] = useState<(Deleting & { key: number }) | null>(null);
  const opened = useRef(0);

  const calendarQ = useQuery({ queryKey: CALENDAR_KEY, queryFn: fetchCalendar });
  const data = calendarQ.data;
  const refresh = () => qc.invalidateQueries({ queryKey: CALENDAR_KEY });

  const open = (draft: SemesterDraft, editing: boolean) =>
    setDialog({ key: ++opened.current, draft, editing });
  // The names ride along, so a save keeps them.
  const openTerm = (t: Term) =>
    open({ kind: "term", code: t.code, nameZh: t.name_zh, nameEn: t.name_en, from: t.start_date, to: t.end_date }, true);
  const openHoliday = (h: Holiday) =>
    open({ kind: "holiday", id: h.id, nameZh: h.name_zh, nameEn: h.name_en, from: h.start_date, to: h.end_date }, true);
  const addTerm = (code = data?.undated_terms[0] ?? "") => open({ kind: "term", code }, false);
  const askDelete = (d: Deleting) => setDeleting({ ...d, key: ++opened.current });

  const saveMut = useMutation({
    mutationFn: ({ draft, value }: { key: number; draft: SemesterDraft; value: SemesterSave }) =>
      draft.kind === "term"
        ? api(`/api/academic-calendar/terms/${encodeURIComponent(draft.code || value.code)}`, {
            method: "PUT",
            json: value,
          })
        : api(`/api/academic-calendar/holidays${draft.id ? `/${draft.id}` : ""}`, {
            method: draft.id ? "PUT" : "POST",
            json: value,
          }),
    onSuccess: (_, { key, draft, value }) => {
      toast.success(draft.kind === "term" ? "Semester saved" : "Holiday saved");
      setDialog((d) => (d?.key === key ? null : d));
      if (draft.kind === "term") setAy(ayOf(value.start_date));
      refresh();
    },
  });

  const deleteMut = useMutation({
    mutationFn: (d: Deleting & { key: number }) =>
      d.kind === "term"
        ? api(`/api/academic-calendar/terms/${encodeURIComponent(d.term.code)}`, { method: "DELETE" })
        : api(`/api/academic-calendar/holidays/${d.holiday.id}`, { method: "DELETE" }),
    onSuccess: (_, d) => {
      toast.success(d.kind === "term" ? "Semester removed" : "Holiday removed");
      setDeleting((current) => (current?.key === d.key ? null : current));
      refresh();
    },
  });

  const terms = data?.terms ?? [];
  const holidays = data?.holidays ?? [];
  const termOf = (day: string) => terms.find((t) => day >= t.start_date && day <= t.end_date);

  const termRows: TermRow[] = [
    ...terms
      .filter((t) => ayOf(t.start_date) === ay)
      .map((t) => ({
        code: t.code,
        term: t,
        status: (today < t.start_date ? "Upcoming" : today > t.end_date ? "Past" : "Current") as Status,
      })),
    ...(data?.undated_terms ?? [])
      .filter((c) => Number(c.slice(0, 3)) === ay)
      .map((code) => ({ code, term: null, status: "No dates" as Status })),
  ];
  const holidayRows: HolidayRow[] = holidays
    .filter((h) => ayOf(h.start_date) === ay)
    .map((h) => {
      const t = termOf(h.start_date);
      return { ...h, during: t ? termLabel(t.code) : "Between terms" };
    });

  const termTable = useTable({ rows: termRows, columns: termColumns(openTerm, askDelete, addTerm), initialSort: { key: "range", dir: "asc" } });
  const holidayTable = useTable({
    rows: holidayRows,
    columns: holidayColumns(openHoliday, askDelete),
    perPage: 5,
    initialSort: { key: "range", dir: "asc" },
  });

  // Years with data, today's, and one empty year ahead to add the next term in.
  const years = [...terms.map((t) => t.start_date), ...holidays.map((h) => h.start_date)].map(ayOf);
  const minAy = Math.min(ayOf(today), ...years);
  const maxAy = Math.max(ayOf(today), ...years) + 1;
  const yearHasData = termRows.length > 0 || holidayRows.length > 0
    || terms.some((t) => t.end_date >= `${ay + 1911}-08-01` && t.start_date <= `${ay + 1912}-07-31`);

  const dialogCodes = data?.undated_terms ?? [];

  return (
    <>
      <PageHeading
        group="Academic"
        title="Semester dates"
        meta="/v3/calendar/semesters · every app"
        actions={
          <>
            <Button variant="line" size="form" onClick={() => open({ kind: "holiday" }, false)}>
              <Plus />
              Holiday
            </Button>
            <Button variant="ink" size="form" onClick={() => addTerm()}>
              <Plus />
              Term
            </Button>
          </>
        }
      />

      {data?.undated_terms.map((code) => (
        <div
          key={code}
          role="status"
          className="flex flex-wrap items-center justify-between gap-3 rounded-xl border border-badge-warning-fg/30 bg-badge-warning px-[18px] py-3.5 text-badge-warning-fg"
        >
          <div className="flex items-center gap-2.5">
            <TriangleAlert className="h-[18px] w-[18px] shrink-0" />
            <span>
              <strong className="font-semibold">{termLabel(code)}</strong> has courses filed but no dates
            </span>
          </div>
          <Button
            variant="line"
            size="bar"
            className="border-badge-warning-fg/40 font-normal text-badge-warning-fg"
            onClick={() => addTerm(code)}
          >
            Add dates
          </Button>
        </div>
      ))}

      {calendarQ.isError && (
        <ErrorAlert className="items-center" onRetry={() => calendarQ.refetch()}>
          Failed to load the calendar: {asMessage(calendarQ.error)}
        </ErrorAlert>
      )}

      <Panel className="flex flex-col gap-2.5 px-6 pb-5 pt-4 phone:px-4">
        <div className="flex flex-wrap items-center justify-between gap-3">
          <div className="flex items-center gap-1">
            <Button variant="quiet" size="icon" aria-label="Previous academic year" className="rounded-lg text-ink-2" disabled={ay <= minAy} onClick={() => setAy(ay - 1)}>
              <ChevronLeft />
            </Button>
            <div className="flex min-w-[190px] flex-col text-center">
              <h2 className="text-[15px] font-semibold">Academic year {ay}</h2>
              <span className="text-xs tabular-nums text-ink-3">
                {rangeText(`${ay + 1911}-08-01`, `${ay + 1912}-07-31`)}
              </span>
            </div>
            <Button variant="quiet" size="icon" aria-label="Next academic year" className="rounded-lg text-ink-2" disabled={ay >= maxAy} onClick={() => setAy(ay + 1)}>
              <ChevronRight />
            </Button>
          </div>
          <div className="flex flex-wrap gap-3.5 text-xs text-ink-2">
            <Legend swatch="h-2.5 w-3.5 rounded-[3px] bg-link">Classes</Legend>
            <Legend swatch="h-2.5 w-3.5 rounded-[3px] bg-brand">Holiday</Legend>
            <Legend swatch="h-3 w-0.5 bg-ink">Today</Legend>
          </div>
        </div>
        {calendarQ.isLoading && <Skeleton className="h-[168px]" />}
        {data && !yearHasData && (
          <EmptyState
            icon={Calendar}
            title={`Nothing dated in academic year ${ay}`}
            action={
              <Button variant="ink" size="form" onClick={() => addTerm()}>
                Add term
              </Button>
            }
          />
        )}
        {data && yearHasData && (
          <Timeline ay={ay} terms={terms} holidays={holidays} today={today} onTerm={openTerm} onHoliday={openHoliday} />
        )}
      </Panel>

      <Panel title="Terms" aside={<span className="text-xs text-ink-3">Academic year {ay}</span>}>
        {calendarQ.isLoading ? (
          <Skeleton className="m-6 h-24" />
        ) : (
          <DataTable table={termTable} rowKey={(r) => r.code} empty="No terms" />
        )}
      </Panel>

      <Panel title="Holidays" aside={<span className="text-xs text-ink-3">Academic year {ay}</span>}>
        {calendarQ.isLoading ? (
          <Skeleton className="m-6 h-24" />
        ) : (
          <DataTable table={holidayTable} rowKey={(r) => String(r.id)} minWidth={760} empty="No holidays" />
        )}
      </Panel>

      {dialog && (
        <SemesterDialog
          key={dialog.key}
          draft={dialog.draft}
          editing={dialog.editing}
          codes={dialogCodes}
          marks={(dialog.draft.kind === "term" ? holidays : terms).map((r) => ({ from: r.start_date, to: r.end_date }))}
          pending={saveMut.isPending}
          error={saveMut.isError && saveMut.variables?.key === dialog.key ? asMessage(saveMut.error) : null}
          onClose={() => setDialog(null)}
          onSave={(value) => saveMut.mutate({ key: dialog.key, draft: dialog.draft, value })}
          onDelete={() => {
            const d = dialog.draft;
            const term = terms.find((t) => t.code === d.code);
            const holiday = holidays.find((h) => h.id === d.id);
            setDialog(null);
            if (d.kind === "term" && term) askDelete({ kind: "term", term });
            if (d.kind === "holiday" && holiday) askDelete({ kind: "holiday", holiday });
          }}
        />
      )}

      {deleting && (
        <DeleteDialog
          deleting={deleting}
          pending={deleteMut.isPending}
          error={deleteMut.isError && deleteMut.variables?.key === deleting.key ? asMessage(deleteMut.error) : null}
          onCancel={() => setDeleting(null)}
          onConfirm={() => deleteMut.mutate(deleting)}
        />
      )}
    </>
  );
}

function Legend({ swatch, children }: { swatch: string; children: string }) {
  return (
    <span className="flex items-center gap-1.5">
      <span aria-hidden="true" className={swatch} />
      {children}
    </span>
  );
}

function RowActions({ label, onEdit, onDelete }: { label: string; onEdit: () => void; onDelete: () => void }) {
  return (
    <div className="flex justify-end gap-1">
      <Button variant="quiet" size="iconSm" aria-label={`Edit ${label}`} title="Edit" className={iconButton} onClick={onEdit}>
        <Pencil />
      </Button>
      <Button variant="quiet" size="iconSm" aria-label={`Delete ${label}`} title="Delete" className={iconButton} onClick={onDelete}>
        <Trash2 />
      </Button>
    </div>
  );
}

function termColumns(
  onEdit: (t: Term) => void,
  onDelete: (d: Deleting) => void,
  onAddDates: (code: string) => void,
): Column<TermRow>[] {
  return [
    {
      key: "label",
      label: "Term",
      sortValue: (r) => r.code,
      className: "whitespace-nowrap",
      cell: (r) => (
        <span className="inline-flex items-center gap-2 whitespace-nowrap font-semibold">
          <span
            aria-hidden="true"
            className={cn("size-2.5 rounded-[3px]", r.term ? "bg-link" : "border-[1.5px] border-dashed border-side-muted")}
          />
          {termLabel(r.code)}
        </span>
      ),
    },
    {
      key: "range",
      label: "Classes",
      sortValue: (r) => r.term?.start_date ?? "9999",
      className: "whitespace-nowrap tabular-nums text-ink-2",
      cell: (r) => (r.term ? rangeText(r.term.start_date, r.term.end_date) : "—"),
    },
    {
      key: "weeks",
      label: "Weeks",
      align: "right",
      hide: "phone",
      sortValue: (r) => (r.term ? weeks(r.term.start_date, r.term.end_date) : -1),
      className: "tabular-nums",
      cell: (r) => (r.term ? weeks(r.term.start_date, r.term.end_date) : "—"),
    },
    {
      key: "status",
      label: "Status",
      hide: "phone",
      sortValue: (r) => r.status,
      filterValue: (r) => r.status,
      filterOptions: STATUSES,
      cell: (r) => <Pill tone={STATUS_TONE[r.status]}>{r.status}</Pill>,
    },
    {
      key: "actions",
      label: "",
      className: "py-1.5 last:pr-4",
      cell: (r) =>
        r.term ? (
          <RowActions
            label={termLabel(r.code)}
            onEdit={() => onEdit(r.term!)}
            onDelete={() => onDelete({ kind: "term", term: r.term! })}
          />
        ) : (
          <div className="flex justify-end">
            <Button variant="line" className="h-[34px] rounded-lg px-3 text-[13px] font-normal" onClick={() => onAddDates(r.code)}>
              Add dates
            </Button>
          </div>
        ),
    },
  ];
}

function holidayColumns(onEdit: (h: Holiday) => void, onDelete: (d: Deleting) => void): Column<HolidayRow>[] {
  return [
    {
      key: "name",
      label: "Name",
      sortValue: (h) => h.name_zh,
      cell: (h) => (
        <>
          <span className="font-medium">{h.name_zh}</span> <span className="text-ink-3">{h.name_en}</span>
        </>
      ),
    },
    {
      key: "range",
      label: "Dates",
      sortValue: (h) => h.start_date,
      className: "whitespace-nowrap tabular-nums text-ink-2",
      cell: (h) => rangeText(h.start_date, h.end_date),
    },
    {
      key: "days",
      label: "Days",
      align: "right",
      hide: "phone",
      sortValue: (h) => daysBetween(h.start_date, h.end_date),
      className: "tabular-nums",
      cell: (h) => daysBetween(h.start_date, h.end_date) + 1,
    },
    {
      key: "during",
      label: "During",
      hide: "phone",
      sortValue: (h) => h.during,
      filterValue: (h) => h.during,
      cell: (h) => <Pill tone={h.during === "Between terms" ? "neutral" : "info"}>{h.during}</Pill>,
    },
    {
      key: "overrides",
      label: "Opted in",
      align: "right",
      hide: "tablet",
      sortValue: (h) => h.overrides,
      className: "tabular-nums text-ink-2",
      cell: (h) => h.overrides,
    },
    {
      key: "actions",
      label: "",
      className: "py-1.5 last:pr-4",
      cell: (h) => (
        <RowActions
          label={h.name_en}
          onEdit={() => onEdit(h)}
          onDelete={() => onDelete({ kind: "holiday", holiday: h })}
        />
      ),
    },
  ];
}

function DeleteDialog({
  deleting,
  pending,
  error,
  onCancel,
  onConfirm,
}: {
  deleting: Deleting;
  pending: boolean;
  error: string | null;
  onCancel: () => void;
  onConfirm: () => void;
}) {
  const isTerm = deleting.kind === "term";
  const [from, to] = isTerm
    ? [deleting.term.start_date, deleting.term.end_date]
    : [deleting.holiday.start_date, deleting.holiday.end_date];
  const days = daysBetween(from, to) + 1;
  const overrides = isTerm ? 0 : deleting.holiday.overrides;
  return (
    <FormDialog
      open
      onOpenChange={(o) => !o && onCancel()}
      width={520}
      title={isTerm ? "Delete this term?" : "Delete this holiday?"}
      footer={
        <>
          <Button variant="line" size="form" onClick={onCancel}>
            Cancel
          </Button>
          <Button variant="danger" size="form" disabled={pending} onClick={onConfirm}>
            Delete
          </Button>
        </>
      }
    >
      {error && <ErrorAlert>{error}</ErrorAlert>}
      <div className="flex items-center gap-3 rounded-xl border border-line px-4 py-3.5">
        <span aria-hidden="true" className={cn("h-9 w-3 shrink-0 rounded", isTerm ? "bg-link" : "bg-brand")} />
        <div className="flex min-w-0 flex-col gap-0.5">
          <span className="font-semibold">
            {isTerm ? termLabel(deleting.term.code) : `${deleting.holiday.name_zh} ${deleting.holiday.name_en}`}
          </span>
          <span className="text-[13px] tabular-nums text-ink-2">
            {rangeText(from, to)} · {isTerm ? `${weeks(from, to)} weeks` : `${days} ${days === 1 ? "day" : "days"}`}
          </span>
        </div>
      </div>
      {overrides > 0 && (
        <div className="flex items-center gap-2.5 rounded-[10px] bg-badge-warning px-3.5 py-3 text-[13px] text-badge-warning-fg">
          <Users className="h-4 w-4 shrink-0" />
          <span>
            {overrides} {overrides === 1 ? "user chose" : "users chose"} reminders on this holiday; their choice goes too
          </span>
        </div>
      )}
    </FormDialog>
  );
}
