import { Trash2, Users } from "lucide-react";
import { shortDate } from "@/lib/academic";
import { Button } from "@/components/ui/button";
import { ErrorAlert, FormDialog, Pill } from "@/components/page";
import { detailOf, firstDay, KIND, reach, type Change, type Course } from "./model";

export function DeleteDialog({
  change,
  course,
  pending,
  error,
  onCancel,
  onConfirm,
}: {
  change: Change;
  course: Course | undefined;
  pending: boolean;
  error: string | null;
  onCancel: () => void;
  onConfirm: () => void;
}) {
  const d = detailOf(change, course);
  const first = firstDay(change);
  const what = d.note || [d.old, d.next].filter(Boolean).join(" → ");
  return (
    <FormDialog
      open
      onOpenChange={(o) => !o && onCancel()}
      width={520}
      title={
        <span className="flex items-center gap-3">
          <span aria-hidden="true" className="flex size-10 shrink-0 items-center justify-center rounded-full bg-badge-danger text-danger">
            <Trash2 className="h-[18px] w-[18px]" />
          </span>
          Delete change?
        </span>
      }
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
      <div className="flex items-center gap-3.5 rounded-xl border border-line px-3.5 py-3">
        <div className="flex size-[52px] shrink-0 flex-col items-center justify-center rounded-[10px] bg-ground">
          <span className="text-[11px] font-semibold uppercase text-ink-3">{shortDate(first).split(" ")[0]}</span>
          <span className="text-xl font-semibold leading-none">{Number(first.slice(8, 10))}</span>
        </div>
        <div className="flex min-w-0 flex-col gap-1">
          <span className="font-semibold">
            <span className="font-mono font-medium">{change.course_no}</span> {course?.name}
          </span>
          <div className="flex flex-wrap items-center gap-1.5">
            <Pill tone={KIND[change.kind].tone}>{KIND[change.kind].label}</Pill>
            <span className="text-[13px] text-ink-2">
              {[what, d.periods.toLowerCase()].filter(Boolean).join(" · ")}
            </span>
          </div>
        </div>
      </div>
      {course && (
        <div className="flex items-center gap-1.5 text-[13px] text-ink-3">
          <Users className="h-[15px] w-[15px]" />
          {reach(course)}
        </div>
      )}
    </FormDialog>
  );
}
