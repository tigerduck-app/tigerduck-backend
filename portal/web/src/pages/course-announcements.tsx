// Course announcements: the Moodle course announcements the backend polls,
// classifies and extracts, and the review items an operator decides.

import { useRef, useState } from "react";
import { Link } from "react-router-dom";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { toast } from "sonner";
import { CalendarClock } from "lucide-react";
import { api } from "@/lib/api";
import { CALENDAR_KEY, fetchCalendar, holidayNames, todayIso } from "@/lib/academic";
import { ago } from "@/lib/format";
import { Button } from "@/components/ui/button";
import { Skeleton } from "@/components/ui/skeleton";
import { EmptyState, ErrorAlert, PageHeading, Panel, Pill } from "@/components/page";
import { asMessage } from "./lists/format";
import { CHANGES_KEY, defaultTerm, termMeta, TermPicker } from "./course-changes";
import { ChangeDialog, type ChangeBody } from "./course-changes/ChangeDialog";
import { changeError, KIND, type Change, type Course } from "./course-changes/model";
import { AnnouncementList, type ListState } from "./course-announcements/AnnouncementList";
import { DetailPanel } from "./course-announcements/DetailPanel";
import type { ItemHandlers } from "./course-announcements/items";
import { asChange, KEY, type Detail, type Item, type Review } from "./course-announcements/model";
import { RejectDialog } from "./course-announcements/RejectDialog";
import { ReviewTable } from "./course-announcements/ReviewPanel";

const APPROVED = { add: "Change added", replace: "Change updated", remove: "Change deleted" } as const;

export function CourseAnnouncementsPage() {
  const qc = useQueryClient();
  const today = todayIso();
  const [picked, setPicked] = useState<string | null>(null);
  const [selected, setSelected] = useState<string | null>(null);
  const [list, setList] = useState<ListState>({ query: "", filter: "all", page: 1 });
  // Announcement text names students; hidden again on every load.
  const [showPii, setShowPii] = useState(false);
  const [editing, setEditing] = useState<{ key: number; item: Item } | null>(null);
  const [rejecting, setRejecting] = useState<{ key: number; item: Item } | null>(null);
  const opened = useRef(0);

  const calendarQ = useQuery({ queryKey: CALENDAR_KEY, queryFn: fetchCalendar });
  const terms = calendarQ.data?.terms ?? [];
  const term = terms.find((t) => t.code === picked) ?? defaultTerm(terms, today);
  const code = term?.code ?? "";
  const enc = encodeURIComponent(code);
  const reviewQ = useQuery({
    queryKey: [KEY, code, "review"],
    queryFn: () => api<Review>(`/api/course-announcements/terms/${enc}/review`),
    enabled: code !== "",
  });
  const coursesQ = useQuery({
    queryKey: [CHANGES_KEY, code, "courses"],
    queryFn: () => api<{ courses: Course[] }>(`/api/course-changes/terms/${enc}/courses`),
    enabled: code !== "",
  });
  const changesQ = useQuery({
    queryKey: [CHANGES_KEY, code, "changes"],
    queryFn: () => api<{ changes: Change[] }>(`/api/course-changes/terms/${enc}/changes`),
    enabled: code !== "",
  });
  const review = reviewQ.data;
  const items = review?.items ?? [];
  const shown = selected ?? items[0]?.announcement_id ?? null;
  const detailQ = useQuery({
    queryKey: [KEY, "announcement", shown],
    queryFn: () => api<Detail>(`/api/course-announcements/announcements/${encodeURIComponent(shown!)}`),
    enabled: shown !== null,
  });

  const courses = new Map((coursesQ.data?.courses ?? []).map((c) => [c.course_no, c]));
  const changes = new Map((changesQ.data?.changes ?? []).map((c) => [c.id, c]));
  const refresh = () => {
    qc.invalidateQueries({ queryKey: [KEY] });
    qc.invalidateQueries({ queryKey: [CHANGES_KEY, code] });
  };

  const approveMut = useMutation({
    mutationFn: (item: Item) => api(`/api/course-announcements/items/${item.id}/approve`, { method: "POST" }),
    onSuccess: (_, item) => {
      toast.success(APPROVED[item.action ?? "add"]);
      refresh();
    },
    onError: (e, item) => {
      toast.error(changeError(e, asChange(item, code).kind));
      refresh();
    },
  });
  const rejectMut = useMutation({
    mutationFn: ({ item, reason }: { key: number; item: Item; reason: string }) =>
      api(`/api/course-announcements/items/${item.id}/reject`, { method: "POST", json: { reason } }),
    onSuccess: (_, { key }) => {
      toast.success("Change rejected");
      setRejecting((r) => (r?.key === key ? null : r));
      refresh();
    },
  });
  const saveMut = useMutation({
    mutationFn: ({ item, body }: { key: number; item: Item; body: ChangeBody }) =>
      item.action === "replace"
        ? api(`/api/course-changes/changes/${item.existing_change_id}`, {
            method: "PUT",
            json: { ...body, item_id: item.id },
          })
        : api(`/api/course-changes/terms/${enc}/changes`, { method: "POST", json: { ...body, item_id: item.id } }),
    onSuccess: (_, { key, item }) => {
      toast.success(APPROVED[item.action ?? "add"]);
      setEditing((d) => (d?.key === key ? null : d));
      refresh();
    },
  });
  const reextractMut = useMutation({
    mutationFn: (id: string) =>
      api(`/api/course-announcements/announcements/${encodeURIComponent(id)}/reextract`, { method: "POST" }),
    onSuccess: () => {
      toast.success("Extraction queued");
      refresh();
    },
    onError: (e) => toast.error(asMessage(e)),
  });

  const handlers: ItemHandlers = {
    onApprove: (item) => approveMut.mutate(item),
    onEdit: (item) => setEditing({ key: ++opened.current, item }),
    onReject: (item) => setRejecting({ key: ++opened.current, item }),
    busy: approveMut.isPending,
  };
  // The editor needs the course's class times, and a replace the change it replaces.
  const canEdit = (item: Item) =>
    item.kind === "course_change" &&
    courses.has(item.course_no) &&
    (item.action === "add" || (item.action === "replace" && changes.has(item.existing_change_id ?? -1)));
  const select = (id: string) => {
    setSelected(id);
    const heading = document.getElementById("detail-h");
    const top = heading?.getBoundingClientRect().top ?? 0;
    if (heading && (top < 0 || top > window.innerHeight)) heading.scrollIntoView({ block: "start" });
  };

  if (calendarQ.isError) {
    return (
      <>
        <PageHeading group="Moodle" title="Course announcements" />
        <ErrorAlert className="items-center" onRetry={() => calendarQ.refetch()}>
          Failed to load the calendar: {asMessage(calendarQ.error)}
        </ErrorAlert>
      </>
    );
  }

  const editCourse = editing && courses.get(editing.item.course_no);
  return (
    <>
      <PageHeading
        group="Moodle"
        title="Course announcements"
        meta={
          term && (
            <span className="inline-flex flex-wrap items-center gap-2 font-sans">
              <span>
                {termMeta(term, today)}
                {review?.polled_at && ` · Polled ${ago(review.polled_at)}`}
              </span>
              {review && !review.auto_apply && (
                <Pill tone="warning" className="px-[7px] py-px text-[11px]">
                  Shadow mode
                </Pill>
              )}
            </span>
          )
        }
        actions={
          term && (
            <TermPicker
              terms={terms}
              term={term}
              onPick={(c) => {
                setPicked(c);
                setSelected(null);
                setList({ query: "", filter: "all", page: 1 });
              }}
            />
          )
        }
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
        <Panel
          title="Review"
          aside={
            review && (
              <span className="text-xs tabular-nums text-ink-3">
                {items.length.toLocaleString("en-US")} pending · {review.decided.toLocaleString("en-US")} decided
              </span>
            )
          }
        >
          {reviewQ.isLoading && <Skeleton className="m-6 h-40" />}
          {reviewQ.isError && (
            <ErrorAlert className="m-6 items-center" onRetry={() => reviewQ.refetch()}>
              Failed to load reviews: {asMessage(reviewQ.error)}
            </ErrorAlert>
          )}
          {review && (
            <ReviewTable
              items={items}
              term={code}
              courses={courses}
              changes={changes}
              selected={shown}
              onSelect={select}
              canEdit={canEdit}
              handlers={handlers}
            />
          )}
        </Panel>
      )}

      {term && (
        <div className="grid grid-cols-[minmax(0,5fr)_minmax(0,7fr)] items-start gap-6 tablet:grid-cols-1">
          <Panel title="Announcements">
            <AnnouncementList
              key={code}
              term={code}
              state={list}
              onState={setList}
              courses={courses}
              selected={shown}
              onSelect={select}
              showPii={showPii}
              onTogglePii={() => setShowPii(!showPii)}
            />
          </Panel>
          {detailQ.isLoading && <Skeleton className="h-96" />}
          {detailQ.isError && (
            <ErrorAlert className="items-center" onRetry={() => detailQ.refetch()}>
              Failed to load the announcement: {asMessage(detailQ.error)}
            </ErrorAlert>
          )}
          {detailQ.data && (
            <DetailPanel
              detail={detailQ.data}
              courses={courses}
              changes={changes}
              showPii={showPii}
              onShowPii={() => setShowPii(true)}
              onSelect={select}
              canEdit={canEdit}
              handlers={handlers}
              onReextract={() => reextractMut.mutate(detailQ.data.id)}
              reextracting={reextractMut.isPending}
            />
          )}
        </div>
      )}

      {editing && editCourse && term && (
        <ChangeDialog
          key={editing.key}
          course={editCourse}
          term={term}
          holidays={holidayNames(calendarQ.data?.holidays ?? [])}
          today={today}
          existing={(changesQ.data?.changes ?? []).filter(
            (c) => c.course_no === editing.item.course_no && c.id !== editing.item.existing_change_id,
          )}
          change={editing.item.action === "replace" ? (changes.get(editing.item.existing_change_id!) ?? null) : null}
          prefill={asChange(editing.item, code)}
          initDay={null}
          pending={saveMut.isPending}
          error={
            saveMut.isError && saveMut.variables?.key === editing.key
              ? changeError(saveMut.error, saveMut.variables.body.kind)
              : null
          }
          onClose={() => setEditing(null)}
          onSave={(body) => saveMut.mutate({ key: editing.key, item: editing.item, body })}
        />
      )}

      {rejecting && (
        <RejectDialog
          title={[
            rejecting.item.course_no,
            courses.get(rejecting.item.course_no)?.name,
            KIND[asChange(rejecting.item, code).kind]?.label,
          ]
            .filter(Boolean)
            .join(" · ")}
          pending={rejectMut.isPending}
          error={rejectMut.isError && rejectMut.variables?.key === rejecting.key ? asMessage(rejectMut.error) : null}
          onCancel={() => setRejecting(null)}
          onReject={(reason) => rejectMut.mutate({ key: rejecting.key, item: rejecting.item, reason })}
        />
      )}
    </>
  );
}
