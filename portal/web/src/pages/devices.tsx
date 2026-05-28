import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { toast } from "sonner";
import { Plus, Search, Smartphone } from "lucide-react";
import { api, ApiError } from "@/lib/api";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent } from "@/components/ui/card";
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
import { Label } from "@/components/ui/label";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";
import { PageHeader } from "@/components/ui/section";
import { Skeleton } from "@/components/ui/skeleton";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs";
import { Textarea } from "@/components/ui/textarea";
import type {
  AddMembersResponse,
  DeviceList,
  DevicesPayload,
  DeviceRow,
} from "@/types/api";

// Sub-tab key → matcher on the device row. Apple `device_class` is
// either "iphone" or "ipad"; Android registers without an apns env so we
// gate on `platform` instead of `device_class` to catch the (rare but
// real) older rows where `device_class` was left blank.
const TABS: Array<{
  key: "iphone" | "ipad" | "android";
  label: string;
  match: (d: DeviceRow) => boolean;
}> = [
  { key: "iphone", label: "iPhone", match: (d) => d.device_class === "iphone" },
  { key: "ipad", label: "iPad", match: (d) => d.device_class === "ipad" },
  {
    key: "android",
    label: "Android",
    match: (d) => d.platform === "android",
  },
];

export function DevicesPage() {
  const [search, setSearch] = useState("");
  const [selected, setSelected] = useState<Set<string>>(new Set());

  const q = useQuery<DevicesPayload>({
    // Include the trimmed search in the key so React Query treats it as
    // a distinct fetch — without that the result would be stuck to the
    // first query's data and the table wouldn't update as you type.
    queryKey: ["devices", search.trim()],
    queryFn: () => {
      const trimmed = search.trim();
      const qs = trimmed
        ? `?limit=500&search=${encodeURIComponent(trimmed)}`
        : "?limit=500";
      return api<DevicesPayload>(`/api/devices${qs}`);
    },
    refetchInterval: 30_000,
  });

  function toggle(id: string) {
    const next = new Set(selected);
    if (next.has(id)) next.delete(id);
    else next.add(id);
    setSelected(next);
  }

  function clearSelection() {
    setSelected(new Set());
  }

  return (
    <div className="space-y-6">
      <PageHeader
        title="Registered devices"
        description="Every row in device_registrations. Newest activity first."
      />

      <div className="flex items-center gap-2">
        <div className="relative flex-1 max-w-md">
          <Search className="pointer-events-none absolute left-2.5 top-1/2 h-4 w-4 -translate-y-1/2 text-muted-foreground" />
          <Input
            placeholder="Search by device_id or user_id…"
            value={search}
            onChange={(e) => setSearch(e.target.value)}
            className="pl-8"
          />
        </div>
        {q.data && (
          <span className="text-xs text-muted-foreground tabular-nums">
            {q.data.total} match{q.data.total === 1 ? "" : "es"}
          </span>
        )}
      </div>

      {selected.size > 0 && (
        <SelectionBar
          selected={selected}
          onClear={clearSelection}
          onAdded={clearSelection}
        />
      )}

      {q.isLoading && <Skeleton className="h-48" />}
      {q.isError && (
        <Card>
          <CardContent className="text-sm text-destructive">
            Failed to load devices: {(q.error as Error).message}
          </CardContent>
        </Card>
      )}
      {q.data && (
        <Tabs defaultValue="iphone" className="space-y-4">
          <TabsList>
            {TABS.map((t) => {
              const count = q.data.items.filter(t.match).length;
              return (
                <TabsTrigger key={t.key} value={t.key}>
                  {t.label}
                  <span className="ml-1.5 text-xs text-muted-foreground">
                    {count}
                  </span>
                </TabsTrigger>
              );
            })}
          </TabsList>
          {TABS.map((t) => {
            const rows = q.data!.items.filter(t.match);
            return (
              <TabsContent key={t.key} value={t.key}>
                <DevicesTable
                  rows={rows}
                  selected={selected}
                  onToggle={toggle}
                />
              </TabsContent>
            );
          })}
        </Tabs>
      )}
    </div>
  );
}

function DevicesTable({
  rows,
  selected,
  onToggle,
}: {
  rows: DeviceRow[];
  selected: Set<string>;
  onToggle: (id: string) => void;
}) {
  if (rows.length === 0) {
    return (
      <Card>
        <CardContent className="flex items-center gap-2 text-sm text-muted-foreground">
          <Smartphone className="h-4 w-4" /> No devices in this category yet.
        </CardContent>
      </Card>
    );
  }
  return (
    <Table>
      <TableHeader>
        <TableRow>
          <TableHead className="w-10"></TableHead>
          <TableHead>Device ID</TableHead>
          <TableHead>User</TableHead>
          <TableHead>Bundle</TableHead>
          <TableHead>APNs env</TableHead>
          <TableHead>Push</TableHead>
          <TableHead>Tokens</TableHead>
          <TableHead>Updated</TableHead>
        </TableRow>
      </TableHeader>
      <TableBody>
        {rows.map((d) => (
          <Row
            key={d.device_id}
            d={d}
            checked={selected.has(d.device_id)}
            onToggle={onToggle}
          />
        ))}
      </TableBody>
    </Table>
  );
}

function Row({
  d,
  checked,
  onToggle,
}: {
  d: DeviceRow;
  checked: boolean;
  onToggle: (id: string) => void;
}) {
  return (
    <TableRow>
      <TableCell>
        <Checkbox
          checked={checked}
          onCheckedChange={() => onToggle(d.device_id)}
        />
      </TableCell>
      <TableCell className="font-mono text-xs">
        <span className="block max-w-[16ch] truncate" title={d.device_id}>
          {d.device_id}
        </span>
      </TableCell>
      <TableCell className="font-mono text-xs">{d.user_id || "—"}</TableCell>
      <TableCell className="font-mono text-xs text-muted-foreground">
        {d.bundle_id || "—"}
      </TableCell>
      <TableCell className="text-sm text-muted-foreground">
        {d.apns_env || "—"}
      </TableCell>
      <TableCell>
        {d.server_push_enabled ? (
          <Badge variant="success">on</Badge>
        ) : (
          <Badge variant="muted">off</Badge>
        )}
      </TableCell>
      <TableCell className="space-x-1">
        {d.has_pts_token ? (
          <Badge variant="default">pts</Badge>
        ) : null}
        {d.has_device_token ? (
          <Badge variant="default">apns</Badge>
        ) : null}
        {!d.has_pts_token && !d.has_device_token ? (
          <Badge variant="muted">none</Badge>
        ) : null}
      </TableCell>
      <TableCell className="text-xs text-muted-foreground">
        {formatTs(d.updated_at)}
      </TableCell>
    </TableRow>
  );
}

function SelectionBar({
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

  return (
    <Card className="border-primary/40 bg-primary/5">
      <CardContent className="flex flex-wrap items-center gap-3 py-3">
        <span className="text-sm font-medium">
          {selected.size} selected
        </span>
        <div className="flex-1" />
        <div className="flex items-center gap-2">
          <Label htmlFor="bulk-list" className="text-xs text-muted-foreground">
            Add to list
          </Label>
          <Select value={listId} onValueChange={setListId}>
            <SelectTrigger id="bulk-list" className="w-56">
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
          <Button variant="ghost" size="sm" onClick={onClear}>
            Clear
          </Button>
        </div>
      </CardContent>

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

function formatTs(ts: string): string {
  try {
    return new Date(ts).toLocaleString();
  } catch {
    return ts;
  }
}

function asMessage(e: unknown): string {
  if (e instanceof ApiError) return `HTTP ${e.status} — ${e.message}`;
  if (e instanceof Error) return e.message;
  return String(e);
}
