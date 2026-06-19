import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { toast } from "sonner";
import { Plus, Search, Smartphone, Trash2 } from "lucide-react";
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

// Sub-tab key → matcher on the device row. v3 reports platform
// `ios`/`ipados` (mapped to device_class iphone/ipad server-side);
// Android registers without an apns env so we gate on `platform`.
const TABS: Array<{
  key: string;
  label: string;
  match: (d: DeviceRow) => boolean;
}> = [
  { key: "iphone", label: "iPhone", match: (d) => d.device_class === "iphone" },
  { key: "ipad", label: "iPad", match: (d) => d.device_class === "ipad" },
  { key: "macos", label: "macOS", match: (d) => d.platform === "macos" },
  { key: "android", label: "Android", match: (d) => d.platform === "android" },
];

const CHART_COLORS = [
  "#3b82f6", "#ef4444", "#22c55e", "#f59e0b", "#8b5cf6",
  "#ec4899", "#14b8a6", "#f97316", "#6366f1", "#84cc16",
];

function MiniPie({ data }: { data: { label: string; value: number; color: string }[] }) {
  const total = data.reduce((s, d) => s + d.value, 0);
  if (total === 0) return null;
  const size = 140;
  const cx = size / 2;
  const cy = size / 2;
  const r = size / 2 - 4;
  if (data.length === 1) {
    return (
      <svg width={size} height={size} viewBox={`0 0 ${size} ${size}`}>
        <circle cx={cx} cy={cy} r={r} fill={data[0].color} />
      </svg>
    );
  }
  let cumAngle = -90;
  return (
    <svg width={size} height={size} viewBox={`0 0 ${size} ${size}`}>
      {data.map((d, i) => {
        const angle = (d.value / total) * 360;
        const startRad = (cumAngle * Math.PI) / 180;
        const endRad = ((cumAngle + angle) * Math.PI) / 180;
        cumAngle += angle;
        const largeArc = angle > 180 ? 1 : 0;
        const x1 = cx + r * Math.cos(startRad);
        const y1 = cy + r * Math.sin(startRad);
        const x2 = cx + r * Math.cos(endRad);
        const y2 = cy + r * Math.sin(endRad);
        return (
          <path key={i} d={`M ${cx} ${cy} L ${x1} ${y1} A ${r} ${r} 0 ${largeArc} 1 ${x2} ${y2} Z`} fill={d.color} />
        );
      })}
    </svg>
  );
}

function StatBlock({ title, counts, total }: { title: string; counts: [string, number][]; total: number }) {
  const chartData = counts.map(([label, value], i) => ({
    label, value, color: CHART_COLORS[i % CHART_COLORS.length],
  }));
  return (
    <div className="space-y-3">
      <h4 className="text-sm font-medium">{title}</h4>
      <div className="flex items-start gap-6">
        <MiniPie data={chartData} />
        <div className="space-y-1 text-xs min-w-0">
          {chartData.map((d) => (
            <div key={d.label} className="flex items-center gap-2">
              <span className="inline-block h-2.5 w-2.5 rounded-sm shrink-0" style={{ backgroundColor: d.color }} />
              <span className="truncate">{d.label}</span>
              <span className="text-muted-foreground ml-auto tabular-nums">
                {d.value} ({total > 0 ? ((d.value / total) * 100).toFixed(0) : 0}%)
              </span>
            </div>
          ))}
        </div>
      </div>
    </div>
  );
}

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
        description="Every device in user_devices (v3). Newest activity first."
      />

      <div className="flex items-center gap-2">
        <div className="relative flex-1 max-w-md">
          <Search className="pointer-events-none absolute left-2.5 top-1/2 h-4 w-4 -translate-y-1/2 text-muted-foreground" />
          <Input
            placeholder="Search by Device ID or Student ID…"
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
            <TabsTrigger value="stats">Statistics</TabsTrigger>
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
          <TabsContent value="stats">
            <DeviceStats items={q.data.items} />
          </TabsContent>
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
          <TableHead>App</TableHead>
          <TableHead>OS</TableHead>
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
      <TableCell className="font-mono text-xs break-all">
        {d.device_id}
      </TableCell>
      <TableCell className="font-mono text-xs">{d.user_id || "—"}</TableCell>
      <TableCell className="text-xs">{d.app_version ?? "—"}</TableCell>
      <TableCell className="text-xs">{d.os_version ?? "—"}</TableCell>
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
        {d.has_fcm_token ? (
          <Badge variant="default">fcm</Badge>
        ) : null}
        {!d.has_pts_token && !d.has_device_token && !d.has_fcm_token ? (
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

const APPLE_PLATFORMS = new Set(["ios", "ipados", "macos", "watchos"]);
const ANDROID_PLATFORMS = new Set(["android", "wearos"]);

function DeviceStats({ items }: { items: DeviceRow[] }) {
  const [platformFilter, setPlatformFilter] = useState("all");

  const platforms = [...new Set(items.map((d) => d.platform))].sort();
  const filtered = items.filter((d) => {
    if (platformFilter === "all") return true;
    if (platformFilter === "apple") return APPLE_PLATFORMS.has(d.platform);
    if (platformFilter === "android") return ANDROID_PLATFORMS.has(d.platform);
    return d.platform === platformFilter;
  });

  const countBy = (key: "os_version" | "app_version") => {
    const map: Record<string, number> = {};
    for (const d of filtered) {
      const v = (key === "os_version" ? `${d.platform} ${d[key] ?? "?"}` : d[key]) ?? "Unknown";
      map[v] = (map[v] ?? 0) + 1;
    }
    return Object.entries(map).sort((a, b) => b[1] - a[1]);
  };

  return (
    <Card>
      <CardContent className="space-y-6 pt-4">
        <Select value={platformFilter} onValueChange={setPlatformFilter}>
          <SelectTrigger className="w-44">
            <SelectValue />
          </SelectTrigger>
          <SelectContent>
            <SelectItem value="all">All Platforms</SelectItem>
            <SelectItem value="apple">All Apple</SelectItem>
            <SelectItem value="android">All Android</SelectItem>
            {platforms.map((p) => (
              <SelectItem key={p} value={p}>{p}</SelectItem>
            ))}
          </SelectContent>
        </Select>
        {filtered.length === 0 ? (
          <p className="text-sm text-muted-foreground">No devices for this platform filter.</p>
        ) : (
          <div className="grid grid-cols-1 md:grid-cols-2 gap-6">
            <StatBlock title="OS Version" counts={countBy("os_version")} total={filtered.length} />
            <StatBlock title="App Version" counts={countBy("app_version")} total={filtered.length} />
          </div>
        )}
      </CardContent>
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
