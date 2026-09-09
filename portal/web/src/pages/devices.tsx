// Registered-device admin. The page component is the route target; the
// table, bulk-action bar, and charts are in ./devices/.

import { useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { useHashTab } from "@/hooks/use-hash-tab";
import { Search } from "lucide-react";
import { api } from "@/lib/api";
import { Card, CardContent } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { PageHeader } from "@/components/ui/section";
import { Skeleton } from "@/components/ui/skeleton";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs";
import type { DevicesPayload } from "@/types/api";
import { ListsPage } from "@/pages/lists";
import { DevicesTable } from "./devices/DevicesTable";
import { SelectionBar } from "./devices/SelectionBar";
import { DeviceStats } from "./devices/charts";
import { TABS } from "./devices/format";

export function DevicesPage() {
  const [search, setSearch] = useState("");
  const [selected, setSelected] = useState<Set<string>>(new Set());
  const [activeTab, setActiveTab] = useHashTab("iphone");

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

      <div className="flex flex-wrap items-center gap-2">
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
        <Tabs value={activeTab} onValueChange={setActiveTab} className="space-y-4">
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
            <TabsTrigger value="lists">Lists</TabsTrigger>
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
          <TabsContent value="lists">
            <ListsPage embedded />
          </TabsContent>
        </Tabs>
      )}
    </div>
  );
}
