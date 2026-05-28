import { useMemo, useState } from "react";
import { Link, useNavigate, useParams } from "react-router-dom";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { toast } from "sonner";
import {
  ArrowLeft,
  Layers,
  Pencil,
  Plus,
  Search,
  Trash2,
  X,
} from "lucide-react";
import { api, ApiError } from "@/lib/api";
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
import { Label } from "@/components/ui/label";
import { PageHeader, Section } from "@/components/ui/section";
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
import type {
  AddMembersResponse,
  DeviceList,
  DeviceListMembersResponse,
  DevicesPayload,
} from "@/types/api";

// ─── ListsPage ────────────────────────────────────────────────────────

export function ListsPage() {
  const qc = useQueryClient();
  const navigate = useNavigate();
  const [createOpen, setCreateOpen] = useState(false);
  const [name, setName] = useState("");
  const [description, setDescription] = useState("");

  const listsQ = useQuery<DeviceList[]>({
    queryKey: ["device-lists"],
    queryFn: () => api<DeviceList[]>("/api/device-lists"),
  });

  const createMut = useMutation({
    mutationFn: () =>
      api<DeviceList>("/api/device-lists", {
        method: "POST",
        json: {
          name: name.trim(),
          description: description.trim() || null,
        },
      }),
    onSuccess: (lst) => {
      toast.success(`Created list ${lst.name}`);
      setCreateOpen(false);
      setName("");
      setDescription("");
      qc.invalidateQueries({ queryKey: ["device-lists"] });
      navigate(`/lists/${lst.id}`);
    },
    onError: (e) => toast.error(asMessage(e)),
  });

  return (
    <div className="space-y-6">
      <PageHeader
        title="Lists"
        description="Named cohorts of devices. Pick one as a custom-push target."
      />

      <div className="flex justify-end">
        <Button onClick={() => setCreateOpen(true)}>
          <Plus className="mr-1 h-4 w-4" />
          New list
        </Button>
      </div>

      {listsQ.isLoading && <Skeleton className="h-40" />}
      {listsQ.isError && (
        <Card>
          <CardContent className="text-sm text-destructive">
            Failed to load lists: {(listsQ.error as Error).message}
          </CardContent>
        </Card>
      )}
      {listsQ.data && listsQ.data.length === 0 && (
        <Card>
          <CardContent className="flex items-center gap-2 text-sm text-muted-foreground">
            <Layers className="h-4 w-4" />
            No lists yet — create one to start cohorting devices.
          </CardContent>
        </Card>
      )}
      {listsQ.data && listsQ.data.length > 0 && (
        <Table>
          <TableHeader>
            <TableRow>
              <TableHead>Name</TableHead>
              <TableHead>Description</TableHead>
              <TableHead className="text-right">Members</TableHead>
              <TableHead>Updated</TableHead>
            </TableRow>
          </TableHeader>
          <TableBody>
            {listsQ.data.map((lst) => (
              <TableRow
                key={lst.id}
                className="cursor-pointer hover:bg-accent/50"
                onClick={() => navigate(`/lists/${lst.id}`)}
              >
                <TableCell className="font-medium">{lst.name}</TableCell>
                <TableCell className="max-w-md truncate text-sm text-muted-foreground">
                  {lst.description || "—"}
                </TableCell>
                <TableCell className="text-right tabular-nums">
                  {lst.member_count}
                </TableCell>
                <TableCell className="text-xs text-muted-foreground">
                  {formatTs(lst.updated_at)}
                </TableCell>
              </TableRow>
            ))}
          </TableBody>
        </Table>
      )}

      <Dialog open={createOpen} onOpenChange={setCreateOpen}>
        <DialogContent>
          <DialogHeader>
            <DialogTitle>New list</DialogTitle>
            <DialogDescription>
              Name is required and must be unique. Description is optional.
            </DialogDescription>
          </DialogHeader>
          <div className="space-y-4">
            <div className="grid gap-1.5">
              <Label htmlFor="list-name">Name</Label>
              <Input
                id="list-name"
                value={name}
                onChange={(e) => setName(e.target.value)}
                maxLength={128}
                autoFocus
              />
            </div>
            <div className="grid gap-1.5">
              <Label htmlFor="list-desc">Description (optional)</Label>
              <Textarea
                id="list-desc"
                rows={3}
                value={description}
                onChange={(e) => setDescription(e.target.value)}
                maxLength={1000}
              />
            </div>
          </div>
          <DialogFooter>
            <Button variant="ghost" onClick={() => setCreateOpen(false)}>
              Cancel
            </Button>
            <Button
              disabled={!name.trim() || createMut.isPending}
              onClick={() => createMut.mutate()}
            >
              Create
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </div>
  );
}

// ─── ListDetailPage ───────────────────────────────────────────────────

export function ListDetailPage() {
  const params = useParams();
  const navigate = useNavigate();
  const qc = useQueryClient();
  const listId = Number(params.id);

  const [editOpen, setEditOpen] = useState(false);
  const [deleteOpen, setDeleteOpen] = useState(false);
  const [editName, setEditName] = useState("");
  const [editDesc, setEditDesc] = useState("");

  const listQ = useQuery<DeviceList>({
    queryKey: ["device-list", listId],
    queryFn: () => api<DeviceList>(`/api/device-lists/${listId}`),
    enabled: Number.isFinite(listId),
  });

  const membersQ = useQuery<DeviceListMembersResponse>({
    queryKey: ["device-list-members", listId],
    queryFn: () =>
      api<DeviceListMembersResponse>(
        `/api/device-lists/${listId}/members?limit=1000`,
      ),
    enabled: Number.isFinite(listId),
  });

  const renameMut = useMutation({
    mutationFn: () =>
      api<DeviceList>(`/api/device-lists/${listId}`, {
        method: "PATCH",
        json: {
          name: editName.trim(),
          description: editDesc.trim() || null,
        },
      }),
    onSuccess: () => {
      toast.success("List updated");
      setEditOpen(false);
      qc.invalidateQueries({ queryKey: ["device-list", listId] });
      qc.invalidateQueries({ queryKey: ["device-lists"] });
    },
    onError: (e) => toast.error(asMessage(e)),
  });

  const deleteMut = useMutation({
    mutationFn: () =>
      api(`/api/device-lists/${listId}`, { method: "DELETE" }),
    onSuccess: () => {
      toast.success("List deleted");
      qc.invalidateQueries({ queryKey: ["device-lists"] });
      navigate("/lists");
    },
    onError: (e) => toast.error(asMessage(e)),
  });

  const removeMut = useMutation({
    mutationFn: (deviceId: string) =>
      api(`/api/device-lists/${listId}/members/${deviceId}`, {
        method: "DELETE",
      }),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["device-list-members", listId] });
      qc.invalidateQueries({ queryKey: ["device-list", listId] });
      qc.invalidateQueries({ queryKey: ["device-lists"] });
    },
    onError: (e) => toast.error(asMessage(e)),
  });

  function openEdit() {
    setEditName(listQ.data?.name ?? "");
    setEditDesc(listQ.data?.description ?? "");
    setEditOpen(true);
  }

  if (!Number.isFinite(listId)) {
    return (
      <Card>
        <CardContent className="text-sm text-destructive">
          Invalid list id.
        </CardContent>
      </Card>
    );
  }

  const memberIds = useMemo(
    () => new Set(membersQ.data?.items.map((m) => m.device_id) ?? []),
    [membersQ.data],
  );

  return (
    <div className="space-y-8">
      <div>
        <Link
          to="/lists"
          className="inline-flex items-center gap-1 text-sm text-muted-foreground hover:text-foreground"
        >
          <ArrowLeft className="h-3.5 w-3.5" />
          All lists
        </Link>
      </div>

      {listQ.isLoading && <Skeleton className="h-24" />}
      {listQ.isError && (
        <Card>
          <CardContent className="text-sm text-destructive">
            {(listQ.error as ApiError).status === 404
              ? "List not found."
              : `Failed: ${(listQ.error as Error).message}`}
          </CardContent>
        </Card>
      )}
      {listQ.data && (
        <PageHeader
          title={listQ.data.name}
          description={
            <span>
              {listQ.data.description || "No description"} ·{" "}
              <span className="tabular-nums">
                {listQ.data.member_count} members
              </span>
            </span>
          }
          actions={
            <>
              <Button variant="outline" size="sm" onClick={openEdit}>
                <Pencil className="mr-1 h-4 w-4" />
                Edit
              </Button>
              <Button
                variant="destructive"
                size="sm"
                onClick={() => setDeleteOpen(true)}
              >
                <Trash2 className="mr-1 h-4 w-4" />
                Delete
              </Button>
            </>
          }
        />
      )}

      <AddMembersPanel listId={listId} excludeIds={memberIds} />

      <Section title="Current members">
        {membersQ.isLoading && <Skeleton className="h-32" />}
        {membersQ.data && membersQ.data.items.length === 0 && (
          <div className="text-sm text-muted-foreground">No members yet.</div>
        )}
        {membersQ.data && membersQ.data.items.length > 0 && (
          <Table>
            <TableHeader>
              <TableRow>
                <TableHead>Device ID</TableHead>
                <TableHead>User</TableHead>
                <TableHead>Platform</TableHead>
                <TableHead>Class</TableHead>
                <TableHead>Added</TableHead>
                <TableHead className="text-right">Action</TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {membersQ.data.items.map((m) => (
                <TableRow key={m.device_id}>
                  <TableCell className="font-mono text-xs">
                    <span
                      className="block max-w-[24ch] truncate"
                      title={m.device_id}
                    >
                      {m.device_id}
                    </span>
                  </TableCell>
                  <TableCell className="font-mono text-xs">
                    {m.user_id || "—"}
                  </TableCell>
                  <TableCell className="text-sm text-muted-foreground">
                    {m.platform}
                  </TableCell>
                  <TableCell className="text-sm text-muted-foreground">
                    {m.device_class || "—"}
                  </TableCell>
                  <TableCell className="text-xs text-muted-foreground">
                    {formatTs(m.added_at)}
                  </TableCell>
                  <TableCell className="text-right">
                    <Button
                      variant="ghost"
                      size="sm"
                      onClick={() => removeMut.mutate(m.device_id)}
                      disabled={removeMut.isPending}
                    >
                      <X className="h-4 w-4" />
                    </Button>
                  </TableCell>
                </TableRow>
              ))}
            </TableBody>
          </Table>
        )}
      </Section>

      <Dialog open={editOpen} onOpenChange={setEditOpen}>
        <DialogContent>
          <DialogHeader>
            <DialogTitle>Edit list</DialogTitle>
          </DialogHeader>
          <div className="space-y-4">
            <div className="grid gap-1.5">
              <Label htmlFor="edit-name">Name</Label>
              <Input
                id="edit-name"
                value={editName}
                onChange={(e) => setEditName(e.target.value)}
                maxLength={128}
              />
            </div>
            <div className="grid gap-1.5">
              <Label htmlFor="edit-desc">Description</Label>
              <Textarea
                id="edit-desc"
                rows={3}
                value={editDesc}
                onChange={(e) => setEditDesc(e.target.value)}
                maxLength={1000}
              />
            </div>
          </div>
          <DialogFooter>
            <Button variant="ghost" onClick={() => setEditOpen(false)}>
              Cancel
            </Button>
            <Button
              disabled={!editName.trim() || renameMut.isPending}
              onClick={() => renameMut.mutate()}
            >
              Save
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>

      <Dialog open={deleteOpen} onOpenChange={setDeleteOpen}>
        <DialogContent>
          <DialogHeader>
            <DialogTitle>Delete list?</DialogTitle>
            <DialogDescription>
              The list itself is removed and every membership goes with it.
              The device registrations themselves are not touched.
            </DialogDescription>
          </DialogHeader>
          <DialogFooter>
            <Button variant="ghost" onClick={() => setDeleteOpen(false)}>
              Cancel
            </Button>
            <Button
              variant="destructive"
              onClick={() => {
                setDeleteOpen(false);
                deleteMut.mutate();
              }}
            >
              Delete
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </div>
  );
}

// ─── AddMembersPanel ──────────────────────────────────────────────────

function AddMembersPanel({
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

// ─── helpers ──────────────────────────────────────────────────────────

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
