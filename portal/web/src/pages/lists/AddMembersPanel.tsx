// Search-and-select panel for adding students to a list.

import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { toast } from "sonner";
import { Search } from "lucide-react";
import { api } from "@/lib/api";
import { Button } from "@/components/ui/button";
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card";
import { Checkbox } from "@/components/ui/checkbox";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import { Skeleton } from "@/components/ui/skeleton";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";
import { Textarea } from "@/components/ui/textarea";
import type { AddMembersResponse, DevicesPayload } from "@/types/api";
import { asMessage } from "./format";

export function AddMembersPanel({
  listId,
  excludeIds,
}: {
  listId: number;
  excludeIds: Set<string>;
}) {
  const qc = useQueryClient();
  const [search, setSearch] = useState("");
  const [selected, setSelected] = useState<Set<string>>(new Set());
  const [pasteOpen, setPasteOpen] = useState(false);
  const [pasted, setPasted] = useState("");

  const searchQ = useQuery<DevicesPayload>({
    queryKey: ["device-search", search],
    queryFn: () =>
      api<DevicesPayload>(
        `/api/devices?limit=100&search=${encodeURIComponent(search)}`,
      ),
    enabled: search.trim().length >= 2,
  });

  const addMut = useMutation({
    mutationFn: (deviceIds: string[]) =>
      api<AddMembersResponse>(`/api/device-lists/${listId}/members`, {
        method: "POST",
        json: { device_ids: deviceIds },
      }),
    onSuccess: (r) => {
      toast.success(
        `Added ${r.added}` +
          (r.already_present ? ` · ${r.already_present} already in list` : "") +
          (r.unknown ? ` · ${r.unknown} unknown` : ""),
      );
      setSelected(new Set());
      setPasted("");
      setPasteOpen(false);
      qc.invalidateQueries({ queryKey: ["device-list-members", listId] });
      qc.invalidateQueries({ queryKey: ["device-list", listId] });
      qc.invalidateQueries({ queryKey: ["device-lists"] });
    },
    onError: (e) => toast.error(asMessage(e)),
  });

  // Hide rows that are already in the list — keeps the operator from
  // adding a no-op selection and avoids the dropdown-of-noise problem
  // when most matches are already members.
  const candidates = (searchQ.data?.items ?? []).filter(
    (d) => !excludeIds.has(d.device_id),
  );

  function toggle(id: string) {
    const next = new Set(selected);
    if (next.has(id)) next.delete(id);
    else next.add(id);
    setSelected(next);
  }

  function handlePaste() {
    const ids = pasted
      .split(/[\s,]+/)
      .map((s) => s.trim())
      .filter(Boolean);
    if (ids.length === 0) return;
    addMut.mutate(ids);
  }

  return (
    <Card>
      <CardHeader>
        <div className="flex items-center gap-2">
          <Search className="h-4 w-4 text-muted-foreground" />
          <CardTitle>Add members</CardTitle>
        </div>
        <CardDescription>
          Search by device_id or user_id (≥ 2 characters), or paste a list of
          IDs.
        </CardDescription>
      </CardHeader>
      <CardContent className="space-y-4">
        <div className="flex gap-2">
          <Input
            placeholder="Search device_id or user_id…"
            value={search}
            onChange={(e) => setSearch(e.target.value)}
          />
          <Button variant="outline" onClick={() => setPasteOpen(true)}>
            Paste IDs
          </Button>
        </div>

        {search.trim().length >= 2 && (
          <div className="space-y-2">
            {searchQ.isLoading && <Skeleton className="h-24" />}
            {searchQ.data && candidates.length === 0 && (
              <div className="text-sm text-muted-foreground">
                No matches (already-in-list rows are hidden).
              </div>
            )}
            {candidates.length > 0 && (
              <>
                <div className="flex items-center justify-between">
                  <div className="text-xs text-muted-foreground">
                    {candidates.length} matches · {selected.size} selected
                  </div>
                  <Button
                    size="sm"
                    disabled={selected.size === 0 || addMut.isPending}
                    onClick={() => addMut.mutate(Array.from(selected))}
                  >
                    Add selected
                  </Button>
                </div>
                <Table>
                  <TableHeader>
                    <TableRow>
                      <TableHead className="w-10"></TableHead>
                      <TableHead>Device ID</TableHead>
                      <TableHead>User</TableHead>
                      <TableHead>Platform</TableHead>
                      <TableHead>Class</TableHead>
                    </TableRow>
                  </TableHeader>
                  <TableBody>
                    {candidates.map((d) => (
                      <TableRow
                        key={d.device_id}
                        className="cursor-pointer"
                        onClick={() => toggle(d.device_id)}
                      >
                        <TableCell>
                          <Checkbox
                            checked={selected.has(d.device_id)}
                            onCheckedChange={() => toggle(d.device_id)}
                            onClick={(e) => e.stopPropagation()}
                          />
                        </TableCell>
                        <TableCell className="font-mono text-xs">
                          <span
                            className="block max-w-[24ch] truncate"
                            title={d.device_id}
                          >
                            {d.device_id}
                          </span>
                        </TableCell>
                        <TableCell className="font-mono text-xs">
                          {d.user_id || "—"}
                        </TableCell>
                        <TableCell className="text-sm text-muted-foreground">
                          {d.platform}
                        </TableCell>
                        <TableCell className="text-sm text-muted-foreground">
                          {d.device_class || "—"}
                        </TableCell>
                      </TableRow>
                    ))}
                  </TableBody>
                </Table>
              </>
            )}
          </div>
        )}
      </CardContent>

      <Dialog open={pasteOpen} onOpenChange={setPasteOpen}>
        <DialogContent>
          <DialogHeader>
            <DialogTitle>Paste device IDs</DialogTitle>
            <DialogDescription>
              One per line, or comma/space separated. Unknown IDs are reported
              as "unknown" and ignored.
            </DialogDescription>
          </DialogHeader>
          <Textarea
            rows={8}
            value={pasted}
            onChange={(e) => setPasted(e.target.value)}
            placeholder="abc-1234-…&#10;def-5678-…"
            className="font-mono text-xs"
          />
          <DialogFooter>
            <Button variant="ghost" onClick={() => setPasteOpen(false)}>
              Cancel
            </Button>
            <Button
              disabled={!pasted.trim() || addMut.isPending}
              onClick={handlePaste}
            >
              Add
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </Card>
  );
}
