// Actions on the selected devices: add them to a list, a new one included,
// or deregister them.

import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { toast } from "sonner";
import { Plus, Trash2 } from "lucide-react";
import { api } from "@/lib/api";
import { cn } from "@/lib/cn";
import { Button } from "@/components/ui/button";
import { Reveal } from "@/components/data-table";
import { DialogIcon, ErrorAlert, FormDialog } from "@/components/page";
import type { AddMembersResponse, DeviceList, DeviceRow } from "@/types/api";
import { ListDialog, type ListBody } from "@/pages/lists/ListDialog";
import { addedLine, errorText } from "@/pages/lists/format";
import { platformIcon, platformName, tokens } from "./format";
import { shortId } from "./DevicesTable";

const plural = (n: number) => `${n} ${n === 1 ? "device" : "devices"}`;

export function SelectionBar({
  selected,
  showPii,
  onClear,
}: {
  selected: DeviceRow[];
  /** The table's eye toggle, which the dialog's student IDs follow. */
  showPii: boolean;
  onClear: () => void;
}) {
  const qc = useQueryClient();
  const [listId, setListId] = useState<number | null>(null);
  const [dialog, setDialog] = useState<"new" | "deregister" | null>(null);
  const ids = selected.map((d) => d.device_id);

  const listsQ = useQuery<DeviceList[]>({
    queryKey: ["device-lists"],
    queryFn: () => api<DeviceList[]>("/api/device-lists"),
  });
  const lists = listsQ.data ?? [];

  const addMut = useMutation({
    mutationFn: (list: DeviceList) =>
      api<AddMembersResponse>(`/api/device-lists/${list.id}/members`, {
        method: "POST",
        json: { device_ids: ids },
      }),
    onSuccess: (r, list) => {
      toast.success(addedLine(r, list.name));
      qc.invalidateQueries({ queryKey: ["device-lists"] });
      qc.invalidateQueries({ queryKey: ["device-list-members", list.id] });
      onClear();
    },
    onError: (e) => toast.error(errorText(e)),
  });

  const createMut = useMutation({
    mutationFn: (body: ListBody) =>
      api<DeviceList>("/api/device-lists", { method: "POST", json: body }),
    onSuccess: (list) => {
      toast.success(`Created list ${list.name}`);
      setDialog(null);
      setListId(list.id);
      qc.invalidateQueries({ queryKey: ["device-lists"] });
    },
  });

  const deregisterMut = useMutation({
    mutationFn: () =>
      api<{ deleted: number }>("/api/devices/deregister", {
        method: "POST",
        json: { device_ids: ids },
      }),
    onSuccess: (r) => {
      toast.success(`Deregistered ${plural(r.deleted)}`);
      setDialog(null);
      qc.invalidateQueries({ queryKey: ["devices"] });
      onClear();
    },
  });

  const picked = lists.find((l) => l.id === listId);
  const open = (which: "new" | "deregister") => {
    createMut.reset();
    deregisterMut.reset();
    setDialog(which);
  };

  return (
    <div
      role="region"
      aria-label="Selected devices"
      className="flex flex-wrap items-center gap-2.5 border-b border-link/25 bg-selected px-6 py-2.5 phone:px-4"
    >
      <span className="font-semibold tabular-nums">
        {selected.length} {selected.length === 1 ? "device" : "devices"} selected
      </span>
      <button
        type="button"
        onClick={onClear}
        className="min-h-8 px-2 text-[13px] text-link hover:text-link-hover"
      >
        Clear
      </button>
      <div className="flex-1 phone:hidden" />
      <span id="add-list-label" className="text-[13px] text-ink-2 phone:hidden">
        Add to list
      </span>
      <div role="radiogroup" aria-labelledby="add-list-label" className="flex flex-wrap gap-1">
        {lists.map((l) => {
          const on = l.id === listId;
          return (
            <button
              key={l.id}
              type="button"
              role="radio"
              aria-checked={on}
              onClick={() => setListId(on ? null : l.id)}
              className={cn(
                "inline-flex min-h-8 max-w-[220px] items-center gap-1.5 rounded-full border px-2.5 text-[13px]",
                on ? "border-ink bg-ink text-on-ink" : "border-link/25 bg-surface text-ink hover:bg-row-hover",
              )}
            >
              <span className="truncate">{l.name}</span>
              <span className={cn("text-xs tabular-nums", on ? "text-side-ink" : "text-ink-3")}>
                {l.member_count}
              </span>
            </button>
          );
        })}
      </div>
      <Button variant="line" size="bar" className="gap-1 px-3 text-[13px] font-normal" onClick={() => open("new")}>
        <Plus className="!size-3.5" />
        New list
      </Button>
      <Button
        variant="ink"
        size="bar"
        className="px-4 text-[13px]"
        disabled={!picked || addMut.isPending}
        onClick={() => picked && addMut.mutate(picked)}
      >
        Add
      </Button>
      <Button variant="danger" size="bar" className="gap-1.5 text-[13px]" onClick={() => open("deregister")}>
        <Trash2 className="!size-3.5" />
        Deregister
      </Button>

      {dialog === "new" && (
        <ListDialog
          list={null}
          meta={`${plural(selected.length)} selected`}
          createLabel="Create list"
          pending={createMut.isPending}
          error={createMut.error}
          onClose={() => setDialog(null)}
          onSave={(body) => createMut.mutate(body)}
        />
      )}

      <FormDialog
        open={dialog === "deregister"}
        onOpenChange={(o) => !o && !deregisterMut.isPending && setDialog(null)}
        width={560}
        icon={<DialogIcon icon={Trash2} danger round />}
        title={`Deregister ${plural(selected.length)}?`}
        meta="Soft delete · push tokens invalidated"
        footer={
          <>
            <Button
              variant="line"
              size="form"
              disabled={deregisterMut.isPending}
              onClick={() => setDialog(null)}
            >
              Cancel
            </Button>
            <Button
              variant="danger"
              size="form"
              disabled={deregisterMut.isPending}
              onClick={() => deregisterMut.mutate()}
            >
              {deregisterMut.isPending ? "Deregistering…" : `Deregister ${plural(selected.length)}`}
            </Button>
          </>
        }
      >
        <div className="grid grid-cols-2 gap-2">
          <div className="flex flex-col gap-0.5 rounded-xl bg-badge-danger px-3.5 py-3 text-badge-danger-fg">
            <span className="text-xs font-medium">Devices removed</span>
            <span className="text-[22px] font-semibold tabular-nums">{selected.length}</span>
          </div>
          <div className="flex flex-col gap-0.5 rounded-xl bg-ground px-3.5 py-3">
            <span className="text-xs font-medium text-ink-3">Push tokens invalidated</span>
            <span className="text-[22px] font-semibold tabular-nums">
              {selected.reduce((sum, d) => sum + tokens(d).length, 0)}
            </span>
          </div>
        </div>
        <ul className="max-h-[232px] overflow-y-auto rounded-xl border border-line">
          {selected.map((d) => {
            const Icon = platformIcon(d.platform);
            return (
              <li
                key={d.device_id}
                className="flex items-center gap-2.5 border-t border-divider px-3.5 py-2.5 first:border-t-0"
              >
                <Icon aria-hidden="true" className="h-4 w-4 shrink-0 text-ink-2" />
                <span className="w-16 shrink-0 text-[13px]">{platformName(d.platform)}</span>
                <span className="font-mono text-[13px]">{shortId(d.device_id)}</span>
                <Reveal shown={showPii} width={110}>
                  <span className="font-mono text-[13px] text-ink-3">{d.student_id ?? "—"}</span>
                </Reveal>
                <span className="ml-auto font-mono text-xs text-ink-3 max-[720px]:hidden">
                  {d.app_version ?? "—"}
                </span>
              </li>
            );
          })}
        </ul>
        {deregisterMut.isError && <ErrorAlert>{errorText(deregisterMut.error)}</ErrorAlert>}
      </FormDialog>
    </div>
  );
}
