// The Status page's user table: student IDs and names stay collapsed until
// the eye reveals them, and a row opens the user's page.

import * as React from "react";
import { useQuery } from "@tanstack/react-query";
import { Link } from "react-router-dom";
import { ChevronRight, Search, Smartphone } from "lucide-react";
import { api } from "@/lib/api";
import { monthDay } from "@/lib/format";
import { platformLabel } from "@/lib/platform";
import { Button } from "@/components/ui/button";
import { Skeleton } from "@/components/ui/skeleton";
import { DataTable, PiiToggle, useTable, type Column } from "@/components/data-table";
import { Dot, EmptyState, ErrorAlert, Panel, Pill } from "@/components/page";
import type { UserRow } from "@/types/api";
import { ACCOUNT, PLATFORM_ICON, ago } from "./format";

const PLATFORM_ORDER = ["ios", "ipados", "macos", "watchos", "android", "wearos", "windows", "web"];
const LIST = new Intl.ListFormat("en", { type: "conjunction" });

// "iOS", "Android", "iOS and Android": the user's platforms, each once.
function platformsOf(u: UserRow): string | null {
  const kinds = [...new Set(u.platforms)].sort(
    (a, b) => PLATFORM_ORDER.indexOf(a) - PLATFORM_ORDER.indexOf(b),
  );
  return kinds.length ? LIST.format(kinds.map(platformLabel)) : null;
}

const minutesAgo = (u: UserRow) =>
  u.last_active_at ? (Date.now() - Date.parse(u.last_active_at)) / 60_000 : Infinity;

function activeBucket(u: UserRow): string | null {
  const m = minutesAgo(u);
  if (m === Infinity) return null;
  return m < 1440 ? "Today" : m < 10_080 ? "This week" : "Earlier";
}

function Highlight({ text, term }: { text: string; term: string }) {
  const at = term ? text.toLowerCase().indexOf(term) : -1;
  if (at < 0) return <>{text}</>;
  return (
    <>
      {text.slice(0, at)}
      <mark className="rounded-[3px] bg-[#FFE7A3] px-px text-[#15171A]">{text.slice(at, at + term.length)}</mark>
      {text.slice(at + term.length)}
    </>
  );
}

export function UsersPanel() {
  const [query, setQuery] = React.useState("");
  const [showPii, setShowPii] = React.useState(false);
  const q = useQuery<{ items: UserRow[]; limit: number }>({
    queryKey: ["users"],
    queryFn: () => api("/api/users"),
    refetchInterval: 60_000,
  });
  const term = query.trim().toLowerCase();

  const columns: Column<UserRow>[] = [
    {
      key: "sid",
      label: "Student ID",
      sortValue: (u) => u.student_id ?? "",
      pii: 132,
      className: "py-2.5 first:pl-3 phone:first:pl-1",
      cell: (u) => (
        <Link
          to={`/users/${u.id}`}
          className="inline-flex min-h-8 items-center font-mono text-[13px] text-ink no-underline"
        >
          <Highlight text={u.student_id ?? "—"} term={term} />
        </Link>
      ),
    },
    {
      key: "name",
      label: "Name",
      sortValue: (u) => u.display_name ?? "",
      pii: 120,
      className: "py-2.5 font-medium",
      cell: (u) => u.display_name ?? "—",
    },
    {
      key: "devices",
      label: "Devices",
      sortValue: (u) => u.platforms.length,
      filterValue: platformsOf,
      className: "py-2.5",
      cell: (u) =>
        u.platforms.length ? (
          <div className="flex items-center gap-1.5">
            {u.platforms.map((p, i) => {
              const Icon = PLATFORM_ICON[p] ?? Smartphone;
              return (
                <span
                  key={i}
                  title={platformLabel(p)}
                  className="flex size-6 items-center justify-center rounded-md bg-ground text-ink-2"
                >
                  <Icon className="h-3.5 w-3.5" />
                </span>
              );
            })}
          </div>
        ) : (
          "—"
        ),
    },
    {
      key: "active",
      label: "Last active",
      sortValue: minutesAgo,
      filterValue: activeBucket,
      filterOptions: ["Today", "This week", "Earlier"],
      className: "whitespace-nowrap py-2.5 text-[13px] tabular-nums text-ink-2",
      cell: (u) => (u.last_active_at ? ago(u.last_active_at) : "—"),
    },
    {
      key: "sync",
      label: "Cloud sync",
      sortValue: (u) => (u.cloud_sync === null ? "" : u.cloud_sync ? "On" : "Off"),
      filterValue: (u) => (u.cloud_sync === null ? null : u.cloud_sync ? "On" : "Off"),
      filterOptions: ["On", "Off"],
      hide: "phone",
      className: "py-2.5",
      cell: (u) =>
        u.cloud_sync === null ? "—" : <Pill tone={u.cloud_sync ? "success" : "neutral"}>{u.cloud_sync ? "On" : "Off"}</Pill>,
    },
    {
      key: "account",
      label: "School account",
      sortValue: (u) => (u.account ? ACCOUNT[u.account].label : ""),
      filterValue: (u) => (u.account ? ACCOUNT[u.account].label : null),
      filterOptions: Object.values(ACCOUNT).map((a) => a.label),
      hide: "phone",
      className: "whitespace-nowrap py-2.5 text-[13px]",
      cell: (u) =>
        u.account ? (
          <span className="inline-flex items-center gap-1.5">
            <Dot tone={ACCOUNT[u.account].tone} />
            {ACCOUNT[u.account].label}
          </span>
        ) : (
          "—"
        ),
    },
    {
      key: "joined",
      label: "Joined",
      sortValue: (u) => u.created_at,
      hide: "phone",
      className: "whitespace-nowrap py-2.5 text-[13px] tabular-nums text-ink-3",
      cell: (u) => monthDay(u.created_at),
    },
    {
      key: "open",
      label: "",
      align: "right",
      className: "py-1.5 last:pr-4",
      cell: (u) => (
        <Link
          to={`/users/${u.id}`}
          aria-label="Open user"
          title="Open user"
          className="inline-flex size-8 items-center justify-center rounded-lg text-ink-3 hover:bg-soft hover:text-ink"
        >
          <ChevronRight className="h-4 w-4" />
        </Link>
      ),
    },
  ];
  const table = useTable({
    rows: q.data?.items ?? [],
    columns,
    initialSort: { key: "active", dir: "asc" },
    search: term,
    match: (u, s) =>
      (u.student_id ?? "").toLowerCase().includes(s) || (u.display_name ?? "").toLowerCase().includes(s),
  });

  return (
    <Panel
      title="Users"
      aside={
        <div className="flex w-[408px] max-w-full items-center gap-2 phone:w-full">
          <PiiToggle shown={showPii} onToggle={() => setShowPii((s) => !s)} />
          <label className="flex h-10 min-w-0 flex-1 items-center gap-2 rounded-[9px] border border-field px-3 text-ink-3">
            <Search className="h-4 w-4 shrink-0" />
            <input
              type="search"
              aria-label="Search by student ID or name"
              placeholder="Student ID or name"
              value={query}
              onChange={(e) => setQuery(e.target.value)}
              className="min-w-0 flex-1 bg-transparent font-mono text-ink outline-none placeholder:text-ink-3"
            />
          </label>
        </div>
      }
    >
      {q.isLoading && <Skeleton className="m-6 h-60" />}
      {q.isError && (
        <ErrorAlert className="m-6 items-center" onRetry={() => q.refetch()}>
          Failed to load users: {(q.error as Error).message}
        </ErrorAlert>
      )}
      {q.data && (
        <DataTable
          table={table}
          rowKey={(u) => u.id}
          minWidth={860}
          showPii={showPii}
          empty={
            <EmptyState
              icon={Search}
              title="No matching users"
              action={
                <Button variant="line" size="bar" onClick={() => setQuery("")}>
                  Clear search
                </Button>
              }
            />
          }
        />
      )}
    </Panel>
  );
}
