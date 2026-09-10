// One student's registered devices, with their push jobs and deliveries.

import { useState } from "react";
import { CloudOff } from "lucide-react";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";
import { StatSection } from "./SyncStats";
import { fmt, platformLabel } from "./format";
import type { PushDeliveryRow, PushJobRow, SyncDevice } from "./types";

export function DevicesCard({ devices, pushJobs, pushDeliveries, studentId }: { devices: SyncDevice[]; pushJobs?: PushJobRow[]; pushDeliveries?: PushDeliveryRow[]; studentId: string }) {
  const [platformFilter, setPlatformFilter] = useState("all");

  const platforms = ["ios", "ipados", "macos", "android"];

  const APPLE_PLATFORMS = ["ios", "ipados", "macos", "watchos"];

  const filtered = devices.filter((d) => {
    if (platformFilter === "all") return true;
    if (platformFilter === "apple") return APPLE_PLATFORMS.includes(d.platform);
    return d.platform === platformFilter;
  });

  // The two apps version independently, so an app version only identifies a
  // release together with the family it shipped from — "2.0.1" is a different
  // build on each. Counting the bare string merged them into one slice.
  // Family, not platform: an iPhone and an iPad run the same Apple build.
  const familyLabel = (p: string) =>
    APPLE_PLATFORMS.includes(p) ? "Apple"
      : p === "android" || p === "wearos" ? "Android"
      : platformLabel(p);

  const countBy = (key: "os_version" | "app_version") => {
    const map: Record<string, number> = {};
    for (const d of filtered) {
      const v = key === "os_version"
        ? `${platformLabel(d.platform)} ${d[key] ?? "?"}`
        : `${familyLabel(d.platform)} ${d[key] ?? "?"}`;
      map[v] = (map[v] ?? 0) + 1;
    }
    // Label breaks ties so the order is stable between renders and the two
    // families stay contiguous rather than interleaving at equal counts.
    return Object.entries(map).sort((a, b) => b[1] - a[1] || a[0].localeCompare(b[0]));
  };

  return (
    <Card>
      <CardHeader>
        <CardTitle className="text-base">Devices ({devices.length})</CardTitle>
      </CardHeader>
      <CardContent>
        <Tabs defaultValue="list">
          <TabsList>
            <TabsTrigger value="list">List</TabsTrigger>
            <TabsTrigger value="stats">Statistics</TabsTrigger>
            <TabsTrigger value="push">Push Queue{pushJobs && pushJobs.length > 0 ? ` (${pushJobs.length})` : ""}</TabsTrigger>
          </TabsList>
          <TabsContent value="list">
            <div className="overflow-x-auto"><Table>
              <TableHeader>
                <TableRow>
                  <TableHead>Platform</TableHead>
                  <TableHead>Device ID</TableHead>
                  <TableHead>App Version</TableHead>
                  <TableHead>OS</TableHead>
                  <TableHead>Last Seen</TableHead>
                  <TableHead>Registered</TableHead>
                </TableRow>
              </TableHeader>
              <TableBody>
                {devices.map((d) => (
                  <TableRow key={d.id}>
                    <TableCell>
                      <div className="flex items-center gap-1.5">
                        <Badge variant="outline">{d.platform}</Badge>
                        {d.cloud_sync_enabled === false && <Badge variant="secondary" className="text-[10px] px-1.5 py-0 gap-0.5 text-muted-foreground"><CloudOff className="h-2.5 w-2.5" />local</Badge>}
                      </div>
                    </TableCell>
                    <TableCell className="font-mono text-xs text-muted-foreground break-all">
                      {d.client_device_id}
                    </TableCell>
                    <TableCell className="text-xs">{d.app_version ?? "—"}</TableCell>
                    <TableCell className="text-xs">{d.os_version ? `${platformLabel(d.platform)} ${d.os_version}` : "—"}</TableCell>
                    <TableCell className="text-xs">{fmt(d.last_seen_at)}</TableCell>
                    <TableCell className="text-xs">{fmt(d.created_at)}</TableCell>
                  </TableRow>
                ))}
              </TableBody>
            </Table></div>
          </TabsContent>
          <TabsContent value="stats">
            <div className="mb-4">
              <Select value={platformFilter} onValueChange={setPlatformFilter}>
                <SelectTrigger className="w-44">
                  <SelectValue />
                </SelectTrigger>
                <SelectContent>
                  <SelectItem value="all">All Platforms</SelectItem>
                  <SelectItem value="apple">All Apple</SelectItem>
                  {platforms.map((p) => (
                    <SelectItem key={p} value={p}>{platformLabel(p)}</SelectItem>
                  ))}
                </SelectContent>
              </Select>
            </div>
            {filtered.length === 0 ? (
              <p className="text-sm text-muted-foreground">No devices for this platform filter.</p>
            ) : (
              <div className="grid grid-cols-1 md:grid-cols-2 gap-6">
                <StatSection title="OS Version" counts={countBy("os_version")} total={filtered.length} />
                <StatSection title="App Version" counts={countBy("app_version")} total={filtered.length} />
              </div>
            )}
          </TabsContent>
          <TabsContent value="push">
            <div className="mb-4 flex flex-wrap gap-2">
              {pushJobs && pushJobs.length > 0 && (
                <>
                  <Button
                    size="sm"
                    variant="outline"
                    onClick={async () => {
                      try {
                        const res = await fetch("/api/moodle/push-tick", { method: "POST" });
                        const data = await res.json();
                        if (!data.ok) alert("Push tick failed: " + (data.error ?? "unknown"));
                      } catch (e) {
                        alert("Push tick request failed");
                      }
                    }}
                  >
                    Force Execute Pipeline
                  </Button>
                  <Button
                    size="sm"
                    variant="outline"
                    onClick={async () => {
                      if (!confirm(`Cancel all ${pushJobs.length} pending push jobs?`)) return;
                      try {
                        const res = await fetch(`/api/moodle/push-clear?student_id=${encodeURIComponent(studentId)}`, { method: "POST" });
                        const data = await res.json();
                        if (!data.ok) alert("Clear failed: " + (data.error ?? "unknown"));
                      } catch (e) {
                        alert("Clear request failed");
                      }
                    }}
                  >
                    Clear Queue
                  </Button>
                </>
              )}
              <Button
                size="sm"
                variant="default"
                onClick={async () => {
                  try {
                    const res = await fetch(`/api/moodle/push-sync-trigger?student_id=${encodeURIComponent(studentId)}`, { method: "POST" });
                    const data = await res.json();
                    if (data.deduplicated) alert("Deduplicated — a sync_trigger already exists in this window");
                    else if (!data.ok) alert("Failed: " + (data.error ?? "unknown"));
                  } catch (e) {
                    alert("Request failed");
                  }
                }}
              >
                Force Sync Push
              </Button>
            </div>
            {!pushJobs || pushJobs.length === 0 ? (
              <p className="text-sm text-muted-foreground py-4">No pending push jobs.</p>
            ) : (
              <div className="space-y-4">
                {pushJobs.map((pj) => {
                  const deliveries = (pushDeliveries ?? []).filter((d) => d.push_job_id === pj.id);
                  const sourceDevice = devices.find((d) => d.id === pj.source_device_id);
                  return (
                    <div key={pj.id} className="border rounded-md p-3 space-y-2">
                      <div className="flex items-center gap-2 flex-wrap">
                        <Badge variant={pj.status === "pending" ? "default" : "secondary"}>{pj.status}</Badge>
                        <span className="font-mono text-xs">{pj.scenario}</span>
                        <span className="text-xs text-muted-foreground ml-auto">
                          #{pj.id} &middot; {pj.attempts}/{pj.max_attempts} attempts &middot; fires {fmt(pj.fire_at)} &middot; created {fmt(pj.created_at)}
                        </span>
                      </div>
                      {sourceDevice && (
                        <p className="text-xs text-muted-foreground">
                          Source: <Badge variant="outline" className="text-[10px] px-1 py-0">{sourceDevice.platform}</Badge> {sourceDevice.client_device_id.slice(0, 8)}...
                        </p>
                      )}
                      {pj.last_error && <p className="text-xs text-destructive">{pj.last_error}</p>}
                      {deliveries.length > 0 && (
                        <div className="overflow-x-auto"><Table>
                          <TableHeader>
                            <TableRow>
                              <TableHead className="text-xs">Target Device</TableHead>
                              <TableHead className="text-xs">Provider</TableHead>
                              <TableHead className="text-xs">Status</TableHead>
                              <TableHead className="text-xs">Attempts</TableHead>
                              <TableHead className="text-xs">Error</TableHead>
                            </TableRow>
                          </TableHeader>
                          <TableBody>
                            {deliveries.map((dl) => {
                              const targetDevice = devices.find((d) => d.id === dl.device_id);
                              return (
                                <TableRow key={dl.id}>
                                  <TableCell className="text-xs">
                                    {targetDevice ? (
                                      <><Badge variant="outline" className="text-[10px] px-1 py-0">{targetDevice.platform}</Badge> {targetDevice.client_device_id.slice(0, 8)}...</>
                                    ) : (
                                      <span className="text-muted-foreground">{dl.device_id?.slice(0, 8) ?? "—"}...</span>
                                    )}
                                  </TableCell>
                                  <TableCell><Badge variant="outline" className="text-[10px]">{dl.provider}</Badge></TableCell>
                                  <TableCell>
                                    <Badge variant={dl.status === "sent" ? "default" : dl.status === "failed" ? "destructive" : "secondary"} className="text-[10px]">
                                      {dl.status}
                                    </Badge>
                                  </TableCell>
                                  <TableCell className="text-xs">{dl.attempts}/{dl.max_attempts}</TableCell>
                                  <TableCell className="text-xs text-destructive">{dl.failure_code ?? "—"}</TableCell>
                                </TableRow>
                              );
                            })}
                          </TableBody>
                        </Table></div>
                      )}
                      {deliveries.length === 0 && (
                        <p className="text-xs text-muted-foreground">
                          {new Date(pj.fire_at) > new Date()
                            ? `Scheduled — fires at ${fmt(pj.fire_at)}`
                            : "Not yet materialized (waiting for pipeline tick)"}
                        </p>
                      )}
                    </div>
                  );
                })}
              </div>
            )}
          </TabsContent>
        </Tabs>
      </CardContent>
    </Card>
  );
}
void DevicesCard; // Kept for reuse; topology UI now handles device display
