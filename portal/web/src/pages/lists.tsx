// Device lists, the custom push targets. Both components are route targets;
// their dialogs are in ./lists/.

import { useState } from "react";
import { Link, useNavigate, useParams } from "react-router-dom";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { toast } from "sonner";
import {
  ChevronLeft,
  Copy,
  List,
  Pencil,
  Plus,
  Search,
  Send,
  Smartphone,
  Trash2,
  TriangleAlert,
  X,
} from "lucide-react";
import { api, ApiError } from "@/lib/api";
import { cn } from "@/lib/cn";
import { copyText } from "@/lib/copy";
import { CHART, CHART_OTHER } from "@/lib/chart";
import { fullDate, monthDay, monthDayTime } from "@/lib/format";
import { platformLabel } from "@/lib/platform";
import { Button } from "@/components/ui/button";
import { Skeleton } from "@/components/ui/skeleton";
import { DataTable, useTable, type Column } from "@/components/data-table";
import {
  DialogIcon,
  EmptyState,
  ErrorAlert,
  FormDialog,
  PageHeading,
  Panel,
} from "@/components/page";
import type {
  AddMembersResponse,
  DeviceList,
  DeviceListMemberRow,
  DeviceListMembersResponse,
} from "@/types/api";
import { classLabel, platformIcon } from "./devices/format";
import { AddMembersDialog } from "./lists/AddMembersDialog";
import { ListDialog, type ListBody } from "./lists/ListDialog";
import { addedLine, errorText } from "./lists/format";

const DAY_MS = 86_400_000;
const AGES = ["This week", "This month", "Older"];
const CLASS_COLOR: Record<string, string> = {
  iphone: CHART[0],
  ipad: CHART[2],
  mac: CHART[6],
  android: CHART[3],
  android_tablet: CHART[4],
};

function age(iso: string): string {
  const days = (Date.now() - new Date(iso).getTime()) / DAY_MS;
  return days < 7 ? "This week" : days < 30 ? "This month" : "Older";
}

const pushLink = (list: DeviceList) => `/custom-push?list=${list.id}`;

function SearchBox({
  label,
  placeholder,
  value,
  onChange,
  className,
}: {
  label: string;
  placeholder: string;
  value: string;
  onChange: (v: string) => void;
  className: string;
}) {
  return (
    <label
      className={cn(
        "flex h-9 max-w-full items-center gap-2 rounded-[9px] border border-field px-3 text-ink-3 phone:w-full",
        className,
      )}
    >
      <Search className="h-[15px] w-[15px] shrink-0" />
      <input
        type="search"
        aria-label={label}
        placeholder={placeholder}
        value={value}
        onChange={(e) => onChange(e.target.value)}
        className="min-w-0 flex-1 bg-transparent text-ink outline-none placeholder:text-ink-3"
      />
    </label>
  );
}

function NoMatch({ title, onClear }: { title: string; onClear: () => void }) {
  return (
    <div className="flex flex-col items-center gap-2.5 py-4">
      <span className="text-[15px] font-semibold text-ink">{title}</span>
      <Button variant="line" size="bar" className="font-normal" onClick={onClear}>
        Clear search and filters
      </Button>
    </div>
  );
}

/** The list editor and delete confirm both pages open, with their requests. */
function useListActions(onDeleted: (list: DeviceList) => void) {
  const qc = useQueryClient();
  const [editing, setEditing] = useState<{ key: number; list: DeviceList | null } | null>(null);
  const [deleting, setDeleting] = useState<DeviceList | null>(null);
  const refresh = () => qc.invalidateQueries({ queryKey: ["device-lists"] });

  const saveMut = useMutation({
    mutationFn: ({ list, body }: { key: number; list: DeviceList | null; body: ListBody }) =>
      list
        ? api<DeviceList>(`/api/device-lists/${list.id}`, { method: "PATCH", json: body })
        : api<DeviceList>("/api/device-lists", { method: "POST", json: body }),
    onSuccess: (saved, { key, list }) => {
      toast.success(list ? "List updated" : `Created list ${saved.name}`);
      setEditing((e) => (e?.key === key ? null : e));
      refresh();
      qc.invalidateQueries({ queryKey: ["device-list", saved.id] });
    },
  });
  const deleteMut = useMutation({
    mutationFn: (list: DeviceList) => api(`/api/device-lists/${list.id}`, { method: "DELETE" }),
    onSuccess: (_, list) => {
      toast.success(`Deleted ${list.name}`);
      setDeleting(null);
      refresh();
      onDeleted(list);
    },
    onError: (e) => toast.error(errorText(e)),
  });

  const edit = (list: DeviceList | null) => {
    saveMut.reset();
    setEditing({ key: Date.now(), list });
  };
  const dialogs = (
    <>
      {editing && (
        <ListDialog
          key={editing.key}
          list={editing.list}
          pending={saveMut.isPending}
          error={saveMut.variables?.key === editing.key ? saveMut.error : null}
          onClose={() => setEditing(null)}
          onSave={(body) => saveMut.mutate({ key: editing.key, list: editing.list, body })}
        />
      )}
      <FormDialog
        open={deleting !== null}
        onOpenChange={(o) => !o && setDeleting(null)}
        width={520}
        icon={<DialogIcon icon={Trash2} danger />}
        title={deleting ? `Delete ${deleting.name}?` : ""}
        footer={
          <>
            <Button variant="line" size="form" onClick={() => setDeleting(null)}>
              Cancel
            </Button>
            <Button
              variant="danger"
              size="form"
              disabled={deleteMut.isPending}
              onClick={() => deleting && deleteMut.mutate(deleting)}
            >
              Delete list
            </Button>
          </>
        }
      />
    </>
  );
  return { edit, remove: setDeleting, dialogs };
}

export function ListsPage() {
  const [query, setQuery] = useState("");
  const actions = useListActions(() => {});

  const listsQ = useQuery<DeviceList[]>({
    queryKey: ["device-lists"],
    queryFn: () => api<DeviceList[]>("/api/device-lists"),
  });
  const lists = listsQ.data;
  const most = Math.max(1, ...(lists ?? []).map((l) => l.member_count));

  const columns: Column<DeviceList>[] = [
    {
      key: "name",
      label: "Name",
      sortValue: (l) => l.name,
      // Takes the room the other columns leave, truncating the name to fit.
      className: "w-full min-w-[140px] max-w-0",
      cell: (l) => (
        <Link to={`/lists/${l.id}`} className="flex min-w-0 flex-col text-ink hover:text-ink">
          <span className="truncate font-mono text-[13px] font-semibold">{l.name}</span>
          <span className="truncate text-xs text-ink-3">{l.description || "—"}</span>
        </Link>
      ),
    },
    {
      key: "members",
      label: "Members",
      align: "right",
      sortValue: (l) => l.member_count,
      cell: (l) => (
        <div className="flex items-center justify-end gap-2.5">
          <div aria-hidden="true" className="h-1.5 w-20 rounded-full bg-divider phone:hidden">
            <div
              className="h-1.5 rounded-full bg-chart-1"
              style={{ width: `${(l.member_count / most) * 100}%` }}
            />
          </div>
          <span className="min-w-8 text-right tabular-nums">{l.member_count}</span>
        </div>
      ),
    },
    {
      key: "updated",
      label: "Updated",
      hide: "phone",
      sortValue: (l) => l.updated_at,
      filterValue: (l) => age(l.updated_at),
      filterOptions: AGES,
      className: "whitespace-nowrap text-[13px] tabular-nums text-ink-2",
      cell: (l) => fullDate(l.updated_at),
    },
    {
      key: "created",
      label: "Created",
      hide: "phone",
      sortValue: (l) => l.created_at,
      className: "whitespace-nowrap text-[13px] tabular-nums text-ink-2",
      cell: (l) => fullDate(l.created_at),
    },
    {
      key: "actions",
      label: "",
      className: "py-1.5 last:pr-4",
      cell: (l) => (
        <div className="flex justify-end gap-0.5">
          <Button asChild variant="quiet" size="icon" className="rounded-lg">
            <Link to={pushLink(l)} aria-label={`Send push to ${l.name}`} title="Send push to this list">
              <Send />
            </Link>
          </Button>
          <Button
            variant="quiet"
            size="icon"
            className="rounded-lg"
            aria-label={`Edit ${l.name}`}
            onClick={() => actions.edit(l)}
          >
            <Pencil />
          </Button>
          <Button
            variant="quiet"
            size="icon"
            className="rounded-lg text-badge-danger-fg hover:text-badge-danger-fg"
            aria-label={`Delete ${l.name}`}
            onClick={() => actions.remove(l)}
          >
            <Trash2 />
          </Button>
        </div>
      ),
    },
  ];
  const term = query.trim().toLowerCase();
  const table = useTable({
    rows: lists ?? [],
    columns,
    initialSort: { key: "updated", dir: "desc" },
    search: term,
    match: (l, s) =>
      l.name.toLowerCase().includes(s) || (l.description ?? "").toLowerCase().includes(s),
  });

  return (
    <>
      <PageHeading
        group="Push"
        title="Device lists"
        actions={
          <Button variant="ink" size="form" onClick={() => actions.edit(null)}>
            <Plus />
            New list
          </Button>
        }
      />
      <Panel
        title="Lists"
        aside={
          <SearchBox
            label="Search lists"
            placeholder="Name or description"
            value={query}
            onChange={setQuery}
            className="w-[260px]"
          />
        }
      >
        {listsQ.isLoading && <Skeleton className="m-6 h-40" />}
        {listsQ.isError && (
          <ErrorAlert className="m-6 items-center" onRetry={() => listsQ.refetch()}>
            Couldn't load lists · {errorText(listsQ.error)}
          </ErrorAlert>
        )}
        {lists && lists.length === 0 && (
          <EmptyState
            icon={List}
            title="No lists yet"
            action={
              <Button variant="line" size="bar" className="font-normal" onClick={() => actions.edit(null)}>
                New list
              </Button>
            }
          />
        )}
        {lists && lists.length > 0 && (
          <DataTable
            table={table}
            rowKey={(l) => String(l.id)}
            minWidth={760}
            empty={
              <NoMatch
                title="No matching lists"
                onClear={() => {
                  setQuery("");
                  table.clearFilters();
                }}
              />
            }
          />
        )}
      </Panel>
      {actions.dialogs}
    </>
  );
}

export function ListDetailPage() {
  const params = useParams();
  const navigate = useNavigate();
  const qc = useQueryClient();
  const listId = Number(params.id);
  const [query, setQuery] = useState("");
  const [adding, setAdding] = useState(false);
  const actions = useListActions(() => navigate("/lists"));

  const listQ = useQuery<DeviceList>({
    queryKey: ["device-list", listId],
    queryFn: () => api<DeviceList>(`/api/device-lists/${listId}`),
    enabled: Number.isFinite(listId),
  });
  const membersQ = useQuery<DeviceListMembersResponse>({
    queryKey: ["device-list-members", listId],
    // shortcut: shows the route's first 2,000 members; page on the server past that.
    queryFn: () => api<DeviceListMembersResponse>(`/api/device-lists/${listId}/members?limit=2000`),
    enabled: Number.isFinite(listId),
  });
  const refresh = () => {
    qc.invalidateQueries({ queryKey: ["device-list-members", listId] });
    qc.invalidateQueries({ queryKey: ["device-list", listId] });
    qc.invalidateQueries({ queryKey: ["device-lists"] });
  };

  const addMut = useMutation({
    mutationFn: (deviceIds: string[]) =>
      api<AddMembersResponse>(`/api/device-lists/${listId}/members`, {
        method: "POST",
        json: { device_ids: deviceIds },
      }),
    onSuccess: refresh,
  });
  const removeMut = useMutation({
    mutationFn: (deviceId: string) =>
      api(`/api/device-lists/${listId}/members/${deviceId}`, { method: "DELETE" }),
    onSuccess: (_, deviceId) => {
      refresh();
      toast.success("Removed 1 device", {
        duration: 5000,
        action: {
          label: "Undo",
          onClick: () =>
            addMut.mutate([deviceId], { onError: (e) => toast.error(errorText(e)) }),
        },
      });
    },
    onError: (e) => toast.error(errorText(e)),
  });

  const members = membersQ.data?.items ?? [];
  const memberIds = new Set(members.map((m) => m.device_id));
  const columns: Column<DeviceListMemberRow>[] = [
    {
      key: "device",
      label: "Device",
      hide: "phone",
      sortValue: (m) => m.device_id,
      className: "py-2",
      cell: (m) => (
        <div className="flex items-center gap-1">
          <span title={m.device_id} className="font-mono text-[13px]">
            {m.device_id.slice(0, 13)}…
          </span>
          <Button
            variant="quiet"
            size="iconSm"
            aria-label="Copy device ID"
            title="Copy"
            className="size-8 [&_svg]:size-3.5"
            onClick={() => copyText(m.device_id, "Copied device ID")}
          >
            <Copy />
          </Button>
        </div>
      ),
    },
    {
      key: "student",
      label: "Student ID",
      sortValue: (m) => m.student_id ?? "",
      className: "font-mono text-[13px]",
      cell: (m) => m.student_id ?? "—",
    },
    {
      key: "name",
      label: "Name",
      sortValue: (m) => m.display_name ?? "",
      className: "whitespace-nowrap font-medium phone:max-w-[120px] phone:truncate",
      cell: (m) => m.display_name ?? "—",
    },
    {
      key: "platform",
      label: "Platform",
      hide: "phone",
      filterValue: (m) => platformLabel(m.platform),
      cell: (m) => {
        const Icon = platformIcon(m.platform);
        return (
          <span className="inline-flex items-center gap-1.5">
            <Icon aria-hidden="true" className="h-[15px] w-[15px]" />
            {platformLabel(m.platform)}
          </span>
        );
      },
    },
    {
      key: "class",
      label: "Class",
      hide: "phone",
      filterValue: (m) => classLabel(m.device_class),
      className: "text-ink-2",
      cell: (m) => classLabel(m.device_class),
    },
    {
      key: "added",
      label: "Added",
      hide: "phone",
      sortValue: (m) => m.added_at,
      className: "whitespace-nowrap text-[13px] tabular-nums text-ink-2",
      cell: (m) => monthDayTime(m.added_at),
    },
    {
      key: "remove",
      label: "",
      className: "py-1.5 text-right last:pr-4",
      cell: (m) => (
        <Button
          variant="quiet"
          size="icon"
          className="rounded-lg"
          aria-label={`Remove ${m.device_id.slice(0, 8)} from list`}
          title="Remove from list"
          disabled={removeMut.isPending}
          onClick={() => removeMut.mutate(m.device_id)}
        >
          <X />
        </Button>
      ),
    },
  ];
  const term = query.trim().toLowerCase();
  const table = useTable({
    rows: members,
    columns,
    initialSort: { key: "added", dir: "desc" },
    search: term,
    match: (m, s) =>
      m.device_id.includes(s) ||
      !!m.student_id?.toLowerCase().includes(s) ||
      !!m.display_name?.toLowerCase().includes(s),
  });

  const list = listQ.data;
  const notFound = listQ.error instanceof ApiError && listQ.error.status === 404;
  if (!Number.isFinite(listId) || notFound) {
    return (
      <Panel title="Device list">
        <div role="alert" className="flex flex-col items-center gap-2.5 px-6 py-12 text-center">
          <div
            aria-hidden="true"
            className="flex size-11 items-center justify-center rounded-full bg-badge-danger text-badge-danger-fg"
          >
            <TriangleAlert className="h-5 w-5" />
          </div>
          <span className="text-[15px] font-semibold">List not found</span>
          <span className="font-mono text-xs text-ink-3">HTTP 404</span>
          <Link to="/lists" className="text-[13px]">
            All lists
          </Link>
        </div>
      </Panel>
    );
  }

  const classes = Object.entries(
    members.reduce<Record<string, number>>((acc, m) => {
      acc[m.device_class] = (acc[m.device_class] ?? 0) + 1;
      return acc;
    }, {}),
  ).sort((a, b) => b[1] - a[1]);

  return (
    <>
      <Link
        to="/lists"
        className="inline-flex min-h-8 items-center gap-1 self-start text-[13px] text-ink-2 hover:text-ink"
      >
        <ChevronLeft className="h-[15px] w-[15px]" />
        All lists
      </Link>

      {listQ.isLoading && <Skeleton className="h-20" />}
      {listQ.isError && !notFound && (
        <ErrorAlert className="items-center" onRetry={() => listQ.refetch()}>
          {errorText(listQ.error)}
        </ErrorAlert>
      )}
      {list && (
        <div className="-mt-3">
          <PageHeading
            group="Device list"
            title={<span className="break-all font-mono">{list.name}</span>}
            meta={
              <span className="font-sans">
                {list.description || "No description"} · updated {monthDay(list.updated_at)}
              </span>
            }
            actions={
              <>
                <Button asChild variant="line" size="form" className="gap-1.5 px-3.5 font-normal">
                  <Link to={pushLink(list)}>
                    <Send className="!size-[15px]" />
                    Send push
                  </Link>
                </Button>
                <Button
                  variant="line"
                  size="form"
                  className="gap-1.5 px-3.5 font-normal"
                  onClick={() => actions.edit(list)}
                >
                  <Pencil className="!size-[15px]" />
                  Edit
                </Button>
                <Button
                  variant="line"
                  size="form"
                  className="gap-1.5 border-badge-danger-fg/25 px-3.5 font-normal text-badge-danger-fg"
                  onClick={() => actions.remove(list)}
                >
                  <Trash2 className="!size-[15px]" />
                  Delete
                </Button>
              </>
            }
          />
        </div>
      )}

      <section
        aria-labelledby="mix-h"
        className="flex flex-col gap-2.5 rounded-[14px] border border-line bg-surface px-6 py-4 phone:px-4"
      >
        <div className="flex flex-wrap items-baseline justify-between gap-2">
          <div className="flex items-baseline gap-2.5">
            <h2 id="mix-h" className="text-[15px] font-semibold">
              Members
            </h2>
            <span className="text-[22px] font-semibold tabular-nums">
              {membersQ.data?.total ?? "—"}
            </span>
          </div>
          <div className="flex flex-wrap gap-3.5 text-xs text-ink-2">
            {classes.map(([cls, n]) => (
              <span key={cls} className="inline-flex items-center gap-1.5">
                <span
                  aria-hidden="true"
                  className="size-2.5 rounded-[2px]"
                  style={{ background: CLASS_COLOR[cls] ?? CHART_OTHER }}
                />
                {classLabel(cls)} <span className="tabular-nums text-ink-3">{n}</span>
              </span>
            ))}
          </div>
        </div>
        {classes.length > 0 && (
          <div className="flex h-3 gap-0.5">
            {classes.map(([cls, n]) => (
              <span
                key={cls}
                title={`${classLabel(cls)} ${n}`}
                className="first:rounded-l last:rounded-r"
                style={{ flex: `${n} 1 0`, background: CLASS_COLOR[cls] ?? CHART_OTHER }}
              />
            ))}
          </div>
        )}
      </section>

      <section aria-label="Member list" className="min-w-0 rounded-[14px] border border-line bg-surface">
        <div className="flex flex-wrap items-center justify-between gap-3 border-b border-line px-6 py-3.5 phone:px-4">
          <SearchBox
            label="Search members"
            placeholder="Device, student ID or name"
            value={query}
            onChange={setQuery}
            className="w-[300px]"
          />
          <Button
            variant="ink"
            size="bar"
            className="gap-1.5"
            onClick={() => {
              addMut.reset();
              setAdding(true);
            }}
          >
            <Plus className="!size-[15px]" />
            Add members
          </Button>
        </div>
        {membersQ.isLoading && <Skeleton className="m-6 h-32" />}
        {membersQ.isError && (
          <ErrorAlert className="m-6 items-center" onRetry={() => membersQ.refetch()}>
            {errorText(membersQ.error)}
          </ErrorAlert>
        )}
        {membersQ.data && members.length === 0 && (
          <EmptyState
            icon={Smartphone}
            title="No members yet"
            action={
              <Button variant="line" size="bar" className="font-normal" onClick={() => setAdding(true)}>
                Add members
              </Button>
            }
          />
        )}
        {members.length > 0 && (
          <DataTable
            table={table}
            rowKey={(m) => m.device_id}
            minWidth={760}
            empty={
              <NoMatch
                title="No matching members"
                onClear={() => {
                  setQuery("");
                  table.clearFilters();
                }}
              />
            }
          />
        )}
      </section>

      {adding && (
        <AddMembersDialog
          memberIds={memberIds}
          pending={addMut.isPending}
          error={addMut.error}
          onClose={() => setAdding(false)}
          onAdd={(ids) =>
            addMut.mutate(ids, {
              onSuccess: (r) => {
                toast.success(addedLine(r));
                setAdding(false);
              },
            })
          }
        />
      )}
      {actions.dialogs}
    </>
  );
}
