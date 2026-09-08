// Bulk actions for the current selection. The destructive ones live here
// together so their confirmation flows stay side by side.

import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { toast } from "sonner";
import { Plus, Trash2 } from "lucide-react";
import { api } from "@/lib/api";
import { Button } from "@/components/ui/button";
import { Card, CardContent } from "@/components/ui/card";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { Textarea } from "@/components/ui/textarea";
import type { AddMembersResponse, DeviceList } from "@/types/api";
import { asMessage } from "./format";

export function SelectionBar({
  selected,
  onClear,
  onAdded,
}: {
  selected: Set<string>;
  onClear: () => void;
  onAdded: () => void;
}) {
  const qc = useQueryClient();
  const [listId, setListId] = useState<string>("");
  const [createOpen, setCreateOpen] = useState(false);
  const [confirmDeregister, setConfirmDeregister] = useState(false);
  const [newName, setNewName] = useState("");
  const [newDesc, setNewDesc] = useState("");

  const listsQ = useQuery<DeviceList[]>({
    queryKey: ["device-lists"],
    queryFn: () => api<DeviceList[]>("/api/device-lists"),
  });

  const addMut = useMutation({
    mutationFn: () =>
      api<AddMembersResponse>(`/api/device-lists/${listId}/members`, {
        method: "POST",
        json: { device_ids: Array.from(selected) },
      }),
    onSuccess: (r) => {
      toast.success(
        `Added ${r.added}` +
          (r.already_present ? ` · ${r.already_present} already in list` : "") +
          (r.unknown ? ` · ${r.unknown} unknown` : ""),
      );
      qc.invalidateQueries({ queryKey: ["device-lists"] });
      qc.invalidateQueries({ queryKey: ["device-list-members"] });
      onAdded();
    },
    onError: (e) => toast.error(asMessage(e)),
  });

  const createMut = useMutation({
    mutationFn: () =>
      api<DeviceList>("/api/device-lists", {
        method: "POST",
        json: {
          name: newName.trim(),
          description: newDesc.trim() || null,
        },
      }),
    onSuccess: (lst) => {
      toast.success(`Created list ${lst.name}`);
      setCreateOpen(false);
      setNewName("");
      setNewDesc("");
      qc.invalidateQueries({ queryKey: ["device-lists"] });
      // Auto-select the freshly created list so the operator can hit
      // "Add" without an extra dropdown click.
      setListId(String(lst.id));
    },
    onError: (e) => toast.error(asMessage(e)),
  });

  const deregisterMut = useMutation({
    mutationFn: () =>
      api<{ deleted: number }>("/api/devices/deregister", {
        method: "POST",
        json: { device_ids: Array.from(selected) },
      }),
    onSuccess: (r) => {
      toast.success(`Deregistered ${r.deleted} device(s)`);
      qc.invalidateQueries({ queryKey: ["devices"] });
      setConfirmDeregister(false);
      onClear();
    },
    onError: (e) => {
      toast.error(asMessage(e));
      setConfirmDeregister(false);
    },
  });

  return (
    <Card className="border-primary/40 bg-primary/5">
      <CardContent className="flex flex-wrap items-center gap-3 py-3">
        <span className="text-sm font-medium">
          {selected.size} selected
        </span>
        <div className="flex-1" />
        <div className="flex flex-wrap items-center gap-2">
          <Label htmlFor="bulk-list" className="text-xs text-muted-foreground">
            Add to list
          </Label>
          <Select value={listId} onValueChange={setListId}>
            <SelectTrigger id="bulk-list" className="w-56 max-w-full">
              <SelectValue placeholder="Pick a list…" />
            </SelectTrigger>
            <SelectContent>
              {(listsQ.data ?? []).map((l) => (
                <SelectItem key={l.id} value={String(l.id)}>
                  {l.name} · {l.member_count}
                </SelectItem>
              ))}
            </SelectContent>
          </Select>
          <Button
            variant="outline"
            size="sm"
            onClick={() => setCreateOpen(true)}
          >
            <Plus className="mr-1 h-4 w-4" />
            New
          </Button>
          <Button
            size="sm"
            disabled={!listId || addMut.isPending}
            onClick={() => addMut.mutate()}
          >
            Add
          </Button>
          <Button
            variant="destructive"
            size="sm"
            onClick={() => setConfirmDeregister(true)}
          >
            <Trash2 className="mr-1 h-3.5 w-3.5" />
            Deregister
          </Button>
          <Button variant="ghost" size="sm" onClick={onClear}>
            Clear
          </Button>
        </div>
      </CardContent>

      <Dialog open={confirmDeregister} onOpenChange={setConfirmDeregister}>
        <DialogContent>
          <DialogHeader>
            <DialogTitle>Deregister {selected.size} device(s)?</DialogTitle>
            <DialogDescription>
              This will soft-delete the selected devices and invalidate all their
              push tokens. The devices will need to re-register on next app
              launch. This action cannot be easily undone.
            </DialogDescription>
          </DialogHeader>
          <div className="flex justify-end gap-2 pt-2">
            <Button
              variant="outline"
              size="sm"
              onClick={() => setConfirmDeregister(false)}
            >
              Cancel
            </Button>
            <Button
              variant="destructive"
              size="sm"
              disabled={deregisterMut.isPending}
              onClick={() => deregisterMut.mutate()}
            >
              {deregisterMut.isPending ? "Deregistering…" : "Confirm deregister"}
            </Button>
          </div>
        </DialogContent>
      </Dialog>

      <Dialog open={createOpen} onOpenChange={setCreateOpen}>
        <DialogContent>
          <DialogHeader>
            <DialogTitle>New list</DialogTitle>
            <DialogDescription>
              Creates the list, then queues the selected devices for adding
              once you click Add.
            </DialogDescription>
          </DialogHeader>
          <div className="space-y-4">
            <div className="grid gap-1.5">
              <Label htmlFor="new-list-name">Name</Label>
              <Input
                id="new-list-name"
                value={newName}
                onChange={(e) => setNewName(e.target.value)}
                maxLength={128}
                autoFocus
              />
            </div>
            <div className="grid gap-1.5">
              <Label htmlFor="new-list-desc">Description (optional)</Label>
              <Textarea
                id="new-list-desc"
                rows={3}
                value={newDesc}
                onChange={(e) => setNewDesc(e.target.value)}
                maxLength={1000}
              />
            </div>
          </div>
          <DialogFooter>
            <Button variant="ghost" onClick={() => setCreateOpen(false)}>
              Cancel
            </Button>
            <Button
              disabled={!newName.trim() || createMut.isPending}
              onClick={() => createMut.mutate()}
            >
              Create
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </Card>
  );
}
