// Announcement: the bulletins the apps list, and the composer that writes one
// or edits a scraped one. The backend's dispatcher pushes a new bulletin to
// the devices whose subscriptions match it.

import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { toast } from "sonner";
import { Megaphone, Pencil, Plus, RefreshCw, Search } from "lucide-react";
import { api, errorText } from "@/lib/api";
import { monthDayTime } from "@/lib/format";
import { Button } from "@/components/ui/button";
import { Skeleton } from "@/components/ui/skeleton";
import { DataTable, useTable, type Column } from "@/components/data-table";
import { EmptyState, ErrorAlert, PageHeading, Panel, Pill } from "@/components/page";
import type { BulletinDetail, BulletinList, BulletinSummary, Taxonomy } from "@/types/api";
import { AnnouncementDialog, IMPORTANCE, type Draft } from "./announcement/AnnouncementDialog";

const IMPORTANCE_RANK = { low: 1, normal: 2, high: 3 } as const;

function optional(s: string): string | undefined {
  return s.trim() || undefined;
}

function createBody(d: Draft): Record<string, unknown> {
  return {
    title: d.title,
    title_clean: optional(d.title_clean),
    summary: optional(d.summary),
    body_clean: optional(d.body_clean),
    body_md: optional(d.body_md),
    canonical_org: d.canonical_org,
    content_tags: d.content_tags,
    importance: d.importance,
    source_url: d.source_url,
  };
}

// Only the changed keys; content_tags always, since an empty list clears them.
function patchBody(d: Draft, b: BulletinDetail): Record<string, unknown> {
  const body: Record<string, unknown> = { content_tags: d.content_tags };
  if (d.title !== b.title) body.title = d.title;
  for (const key of ["title_clean", "summary", "body_clean", "body_md"] as const) {
    if (d[key] !== (b[key] ?? "")) body[key] = optional(d[key]) ?? null;
  }
  if (d.canonical_org !== b.canonical_org) body.canonical_org = d.canonical_org;
  if (d.importance !== b.importance) body.importance = d.importance;
  return body;
}

export function AnnouncementPage() {
  const qc = useQueryClient();
  const [query, setQuery] = useState("");
  // null: closed; `bulletin` null for a new one.
  const [editing, setEditing] = useState<{ bulletin: BulletinDetail | null } | null>(null);

  const taxonomyQ = useQuery<Taxonomy>({
    queryKey: ["taxonomy"],
    queryFn: () => api<Taxonomy>("/api/announcement/taxonomy"),
    staleTime: Infinity,
  });
  // shortcut: the table holds the latest 100 bulletins; page by cursor once operators need older ones.
  const listQ = useQuery<BulletinList>({
    queryKey: ["bulletins-list"],
    queryFn: () => api<BulletinList>("/api/announcement/list?limit=100&include_deleted=true"),
  });

  const saveMut = useMutation({
    mutationFn: ({ d, original }: { d: Draft; original: BulletinDetail | null }) =>
      original
        ? api<BulletinDetail>(`/api/announcement/${original.id}`, {
            method: "PATCH",
            json: patchBody(d, original),
          })
        : api<BulletinDetail>("/api/announcement", { method: "POST", json: createBody(d) }),
    onSuccess: (b, { original }) => {
      toast.success(original ? `Updated #${b.id}` : `Broadcast #${b.id}`);
      setEditing(null);
      qc.invalidateQueries({ queryKey: ["bulletins-list"] });
    },
    onError: (e) => toast.error(errorText(e)),
  });

  const orgs = taxonomyQ.data?.orgs ?? [];
  const tags = taxonomyQ.data?.tags ?? [];
  const orgLabel = (b: BulletinSummary) =>
    b.canonical_org ? (orgs.find((o) => o.id === b.canonical_org)?.label ?? b.canonical_org) : null;
  const tagLabel = (id: string) => tags.find((t) => t.id === id)?.label ?? id;
  const shownTitle = (b: BulletinSummary) => b.title_clean || b.title;
  const open = async (id: number | null) => {
    saveMut.reset();
    if (id === null) return setEditing({ bulletin: null });
    try {
      setEditing({ bulletin: await api<BulletinDetail>(`/api/announcement/${id}`) });
    } catch (e) {
      toast.error(errorText(e));
    }
  };

  const columns: Column<BulletinSummary>[] = [
    {
      key: "id",
      label: "#",
      sortValue: (b) => b.id,
      hide: "phone",
      className: "font-mono text-[13px] tabular-nums text-ink-3",
      cell: (b) => `#${b.id}`,
    },
    {
      key: "title",
      label: "Title",
      sortValue: shownTitle,
      className: "w-full min-w-[220px] max-w-0 phone:min-w-[120px]",
      cell: (b) => (
        <button type="button" onClick={() => open(b.id)} className="block w-full text-left">
          <span className="flex items-center gap-1.5">
            <span className="truncate font-medium">{shownTitle(b)}</span>
            {b.is_deleted && <Pill className="shrink-0 px-[7px] py-px text-[11px]">Deleted</Pill>}
          </span>
          <span className="block truncate text-xs text-ink-3">{b.summary || "—"}</span>
        </button>
      ),
    },
    {
      key: "org",
      label: "Org",
      filterValue: orgLabel,
      hide: "phone",
      className: "whitespace-nowrap",
      cell: (b) => orgLabel(b) ?? "—",
    },
    {
      key: "importance",
      label: "Importance",
      sortValue: (b) => (b.importance ? IMPORTANCE_RANK[b.importance] : 0),
      filterValue: (b) => (b.importance ? IMPORTANCE[b.importance].label : null),
      filterOptions: ["High", "Normal", "Low"],
      hide: "phone",
      cell: (b) => {
        if (!b.importance) return "—";
        const { label, icon: Icon, tone, className } = IMPORTANCE[b.importance];
        return (
          <Pill tone={tone} className={className}>
            <Icon aria-hidden="true" className="size-3" strokeWidth={2.4} />
            {label}
          </Pill>
        );
      },
    },
    {
      key: "tags",
      label: "Tags",
      hide: "tablet",
      cell: (b) => (
        <div className="flex max-w-[200px] flex-wrap gap-1">
          {b.content_tags.map((t) => (
            <span key={t} className="whitespace-nowrap rounded-full border border-line bg-ground px-2 py-px text-xs text-ink-2">
              {tagLabel(t)}
            </span>
          ))}
        </div>
      ),
    },
    {
      key: "source",
      label: "Source",
      filterValue: (b) => b.source,
      hide: "tablet",
      cell: (b) => <Pill tone={b.source === "manual" ? "info" : "neutral"}>{b.source}</Pill>,
    },
    {
      key: "posted",
      label: "Posted",
      sortValue: (b) => b.posted_at ?? "",
      className: "whitespace-nowrap text-[13px] tabular-nums text-ink-2",
      cell: (b) => (b.posted_at ? monthDayTime(b.posted_at) : "—"),
    },
    {
      key: "actions",
      label: "",
      className: "py-1.5 last:pr-4",
      cell: (b) => (
        <Button variant="quiet" size="icon" aria-label={`Edit bulletin ${b.id}`} onClick={() => open(b.id)}>
          <Pencil />
        </Button>
      ),
    },
  ];

  const items = listQ.data?.items;
  const table = useTable({
    rows: items ?? [],
    columns,
    perPage: 8,
    initialSort: { key: "posted", dir: "desc" },
    search: query.trim().toLowerCase(),
    match: (b, term) => shownTitle(b).toLowerCase().includes(term) || `#${b.id}`.includes(term),
  });

  const newButton = (
    <Button variant="ink" size="form" onClick={() => open(null)}>
      <Plus />
      New announcement
    </Button>
  );

  return (
    <>
      <PageHeading
        group="Push"
        title="Announcement"
        actions={
          <>
            <Button
              variant="line"
              size="form"
              disabled={listQ.isFetching}
              onClick={async () => {
                if ((await listQ.refetch()).isSuccess) toast.success("Bulletins refreshed");
              }}
            >
              <RefreshCw />
              Refresh
            </Button>
            {newButton}
          </>
        }
      />

      <Panel
        title="Bulletins"
        aside={
          <label className="flex h-9 w-[280px] max-w-full items-center gap-2 rounded-[9px] border border-field px-3 text-ink-3 phone:w-full">
            <Search className="h-[15px] w-[15px] shrink-0" />
            <input
              type="search"
              aria-label="Search bulletins"
              placeholder="Title or #"
              value={query}
              onChange={(e) => setQuery(e.target.value)}
              className="min-w-0 flex-1 bg-transparent text-ink outline-none placeholder:text-ink-3"
            />
          </label>
        }
      >
        {listQ.isLoading && <Skeleton className="m-6 h-40" />}
        {listQ.isError && (
          <ErrorAlert className="m-6 items-center" onRetry={() => listQ.refetch()}>
            Couldn’t load bulletins
            <div className="font-mono text-xs">{errorText(listQ.error)}</div>
          </ErrorAlert>
        )}
        {items && items.length === 0 && <EmptyState icon={Megaphone} title="No bulletins yet" action={newButton} />}
        {items && items.length > 0 && (
          <DataTable
            table={table}
            rowKey={(b) => String(b.id)}
            minWidth={920}
            empty={
              <div className="flex flex-col items-center gap-2.5">
                <span className="text-[15px] font-semibold text-ink">No matching bulletins</span>
                <Button
                  variant="line"
                  size="bar"
                  onClick={() => {
                    setQuery("");
                    table.clearFilters();
                  }}
                >
                  Clear search and filters
                </Button>
              </div>
            }
          />
        )}
      </Panel>

      {editing && taxonomyQ.data && (
        <AnnouncementDialog
          key={editing.bulletin?.id ?? "new"}
          bulletin={editing.bulletin}
          taxonomy={taxonomyQ.data}
          pending={saveMut.isPending}
          onClose={() => setEditing(null)}
          onSubmit={(d) => saveMut.mutate({ d, original: editing.bulletin })}
        />
      )}
    </>
  );
}
