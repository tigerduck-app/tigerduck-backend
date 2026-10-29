import { useState } from "react";
import { List, TriangleAlert } from "lucide-react";
import { Button } from "@/components/ui/button";
import { DialogIcon, ErrorAlert, FormDialog, fieldClass } from "@/components/page";
import { ApiError } from "@/lib/api";
import { cn } from "@/lib/cn";
import type { DeviceList } from "@/types/api";
import { errorText } from "./format";

export type ListBody = { name: string; description: string | null };

/** New list when `list` is null, otherwise edits it. `meta` replaces the icon. */
export function ListDialog({
  list,
  meta,
  createLabel = "Create",
  pending,
  error,
  onClose,
  onSave,
}: {
  list: DeviceList | null;
  meta?: string;
  createLabel?: string;
  pending: boolean;
  error: unknown;
  onClose: () => void;
  onSave: (body: ListBody) => void;
}) {
  const [name, setName] = useState(list?.name ?? "");
  const [description, setDescription] = useState(list?.description ?? "");
  // The last save's error stands until the name is edited again.
  const [dirty, setDirty] = useState(false);
  const shown = dirty ? null : error;
  const nameError = shown instanceof ApiError && shown.status === 409;
  const canSave = name.trim().length > 0 && !pending;

  return (
    <FormDialog
      open
      onOpenChange={(open) => !open && onClose()}
      width={560}
      icon={meta ? undefined : <DialogIcon icon={List} />}
      title={list ? "Edit list" : "New list"}
      meta={meta}
      footer={
        <>
          <Button variant="line" size="form" onClick={onClose}>
            Cancel
          </Button>
          <Button
            variant="ink"
            size="form"
            disabled={!canSave}
            onClick={() => {
              setDirty(false);
              onSave({ name: name.trim(), description: description.trim() || null });
            }}
          >
            {list ? "Save" : createLabel}
          </Button>
        </>
      }
    >
      <label className="flex flex-col gap-1.5 text-[13px] font-medium">
        <span className="flex justify-between">
          Name
          <span className="font-normal tabular-nums text-ink-3">{name.length} / 128</span>
        </span>
        <input
          value={name}
          onChange={(e) => {
            setName(e.target.value);
            setDirty(true);
          }}
          maxLength={128}
          autoFocus
          aria-invalid={nameError}
          className={cn(fieldClass, "font-mono text-[13px] font-normal", nameError && "border-dot-bad")}
        />
        {nameError && (
          <span role="alert" className="flex items-center gap-1.5 text-xs font-medium text-badge-danger-fg">
            <TriangleAlert className="h-[13px] w-[13px]" />
            Name already in use
          </span>
        )}
      </label>
      <label className="flex flex-col gap-1.5 text-[13px] font-medium">
        <span className="flex justify-between">
          <span>
            Description <span className="font-normal text-ink-3">optional</span>
          </span>
          <span className="font-normal tabular-nums text-ink-3">{description.length} / 1000</span>
        </span>
        <textarea
          rows={3}
          value={description}
          onChange={(e) => setDescription(e.target.value)}
          maxLength={1000}
          className="resize-y rounded-[9px] border border-field bg-surface px-3 py-2.5 text-sm font-normal text-ink"
        />
      </label>
      {!!shown && !nameError && <ErrorAlert>{errorText(shown)}</ErrorAlert>}
    </FormDialog>
  );
}
