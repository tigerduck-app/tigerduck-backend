// Registered devices: the table with its selection actions, and a
// Statistics view. Parts are in ./devices/.

import { useState } from "react";
import { Link } from "react-router-dom";
import { useQuery } from "@tanstack/react-query";
import { List, RefreshCw, Search, TriangleAlert } from "lucide-react";
import { api } from "@/lib/api";
import { cn } from "@/lib/cn";
import { useHashTab } from "@/hooks/use-hash-tab";
import { Button } from "@/components/ui/button";
import { Skeleton } from "@/components/ui/skeleton";
import { DataTable, PiiToggle, useTable } from "@/components/data-table";
import { PageHeading, Panel } from "@/components/page";
import type { DevicesPayload } from "@/types/api";
import { errorText } from "./lists/format";
import { DeviceStats } from "./devices/charts";
import { deviceColumns, selectColumn } from "./devices/DevicesTable";
import { PLATFORMS, platformName } from "./devices/format";
import { Segmented } from "./devices/Segmented";
import { SelectionBar } from "./devices/SelectionBar";

const VIEWS = [
  ["list", "Devices"],
  ["stats", "Statistics"],
] as const;

export function DevicesPage() {
  const [hash, setView] = useHashTab("list");
  const view = hash === "stats" ? "stats" : "list";
  const [query, setQuery] = useState("");
  const [showPii, setShowPii] = useState(false);
  const [selected, setSelected] = useState<Set<string>>(new Set());

  const q = useQuery<DevicesPayload>({
    queryKey: ["devices"],
    // shortcut: loads at most the route's 10,000 devices; page on the server past that.
    queryFn: () => api<DevicesPayload>("/api/devices?limit=10000"),
    refetchInterval: 30_000,
  });
  const devices = q.data?.items ?? [];

  const term = query.trim().toLowerCase();
  const table = useTable({
    rows: devices,
    columns: deviceColumns(devices),
    perPage: 12,
    initialSort: { key: "updated", dir: "desc" },
    search: term,
    match: (d, s) =>
      d.device_id.includes(s) ||
      !!d.student_id?.toLowerCase().includes(s) ||
      !!d.display_name?.toLowerCase().includes(s),
  });
  const shown = {
    ...table,
    columns: [selectColumn(table.rows, selected, setSelected), ...table.columns],
  };
  // A new search or filter clears the selection, so the actions only reach rows in view.
  const scope = JSON.stringify([term, table.filters]);
  const [selectedScope, setSelectedScope] = useState(scope);
  if (selectedScope !== scope) {
    setSelectedScope(scope);
    setSelected(new Set());
  }
  const chosenRows = devices.filter((d) => selected.has(d.device_id));
  const platformFilter = table.filters.platform ?? [];
  const activePlatform = platformFilter.length === 1 ? platformFilter[0] : "All";

  const loadError = q.isError && !q.data && (
    <div role="alert" className="flex flex-col items-center gap-2.5 px-6 py-14 text-center">
      <div
        aria-hidden="true"
        className="flex size-11 items-center justify-center rounded-full bg-badge-danger text-badge-danger-fg"
      >
        <TriangleAlert className="h-5 w-5" />
      </div>
      <span className="text-[15px] font-semibold">Couldn't load devices</span>
      <span className="font-mono text-xs text-ink-3">{errorText(q.error)}</span>
      <Button variant="line" size="bar" className="gap-1.5 font-normal" onClick={() => q.refetch()}>
        <RefreshCw className="!size-3.5" />
        Retry
      </Button>
    </div>
  );

  return (
    <>
      <PageHeading
        group="Devices"
        title="Devices"
        meta={
          q.data && (
            <span className="font-sans tabular-nums">
              {q.data.total} devices · refreshes every 30 s
            </span>
          )
        }
        actions={
          <>
            <Button asChild variant="line" size="bar" className="gap-1.5 font-normal">
              <Link to="/lists">
                <List className="!size-[15px]" />
                Device lists
              </Link>
            </Button>
            <Segmented label="View" options={VIEWS} value={view} onChange={setView} />
          </>
        }
      />

      {view === "list" && (
        <Panel
          title="Registered devices"
          aside={
            <div className="flex flex-wrap items-center gap-2.5 phone:w-full">
              <div role="group" aria-label="Platform" className="flex flex-wrap gap-1 phone:hidden">
                {["All", ...PLATFORMS].map((p) => {
                  const on = p === activePlatform;
                  const count =
                    p === "All"
                      ? devices.length
                      : devices.filter((d) => platformName(d.platform) === p).length;
                  return (
                    <button
                      key={p}
                      type="button"
                      aria-pressed={on}
                      onClick={() => table.setFilter("platform", p === "All" ? [] : [p])}
                      className={cn(
                        "inline-flex min-h-8 items-center gap-1.5 rounded-full border px-2.5 text-[13px]",
                        on ? "border-ink bg-ink text-on-ink" : "border-field bg-surface text-ink hover:bg-row-hover",
                      )}
                    >
                      {p}
                      <span className={cn("text-xs tabular-nums", on ? "text-side-ink" : "text-ink-3")}>
                        {count}
                      </span>
                    </button>
                  );
                })}
              </div>
              <PiiToggle shown={showPii} onToggle={() => setShowPii((v) => !v)} />
              <label className="flex h-9 w-[280px] max-w-full items-center gap-2 rounded-[9px] border border-field px-3 text-ink-3 phone:w-auto phone:min-w-0 phone:flex-1">
                <Search className="h-[15px] w-[15px] shrink-0" />
                <input
                  type="search"
                  aria-label="Search devices"
                  placeholder="Device ID, student ID or name"
                  value={query}
                  onChange={(e) => setQuery(e.target.value)}
                  className="min-w-0 flex-1 bg-transparent text-[13px] text-ink outline-none placeholder:text-ink-3"
                />
              </label>
            </div>
          }
        >
          {chosenRows.length > 0 && (
            <SelectionBar selected={chosenRows} onClear={() => setSelected(new Set())} />
          )}
          {q.isLoading && <Skeleton className="m-6 h-48" />}
          {loadError}
          {q.data && (
            <DataTable
              table={shown}
              rowKey={(d) => d.device_id}
              minWidth={980}
              showPii={showPii}
              selected={(d) => selected.has(d.device_id)}
              empty={
                <div className="flex flex-col items-center gap-2.5 py-4">
                  <div
                    aria-hidden="true"
                    className="flex size-11 items-center justify-center rounded-full bg-ground text-ink-3"
                  >
                    <Search className="h-5 w-5" />
                  </div>
                  <span className="text-[15px] font-semibold text-ink">No devices match</span>
                  <Button
                    variant="line"
                    size="bar"
                    className="font-normal"
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
      )}

      {view === "stats" && q.data && <DeviceStats items={devices} />}
      {view === "stats" && loadError && <Panel>{loadError}</Panel>}
      {view === "stats" && q.isLoading && <Skeleton className="h-64" />}
    </>
  );
}
