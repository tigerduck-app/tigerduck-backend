import { useState } from "react";
import { Button } from "@/components/ui/button";
import { ErrorAlert, FormDialog } from "@/components/page";

const REASON_MAX = 500;

/** Rejects a review item, with an optional reason kept on it. */
export function RejectDialog({
  title,
  pending,
  error,
  onCancel,
  onReject,
}: {
  /** The course and change, as the review row names them. */
  title: string;
  pending: boolean;
  error: string | null;
  onCancel: () => void;
  onReject: (reason: string) => void;
}) {
  const [reason, setReason] = useState("");
  return (
    <FormDialog
      open
      onOpenChange={(o) => !o && onCancel()}
      width={520}
      title="Reject change"
      meta={title}
      footer={
        <>
          <Button variant="line" size="form" onClick={onCancel}>
            Cancel
          </Button>
          <Button variant="danger" size="form" disabled={pending} onClick={() => onReject(reason.trim())}>
            Reject
          </Button>
        </>
      }
    >
      <label className="flex flex-col gap-1.5 text-[13px] font-medium">
        <span className="flex justify-between">
          <span>
            Reason <span className="font-normal text-ink-3">optional</span>
          </span>
          <span className="font-normal tabular-nums text-ink-3">
            {reason.length} / {REASON_MAX}
          </span>
        </span>
        <textarea
          rows={3}
          value={reason}
          onChange={(e) => setReason(e.target.value)}
          maxLength={REASON_MAX}
          className="resize-y rounded-[9px] border border-field bg-surface px-3 py-2.5 text-sm font-normal text-ink"
        />
      </label>
      {error && <ErrorAlert>{error}</ErrorAlert>}
    </FormDialog>
  );
}
