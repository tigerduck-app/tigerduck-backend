// The graph view of a student's account: devices, courses, and the sync
// records connecting them. Selecting a node opens NodeDetailPanel.

import { CloudOff, Server, Wifi, WifiOff } from "lucide-react";
import { Badge } from "@/components/ui/badge";
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card";
import { fmt, platformIcon, platformLabel, relativeTime, sortedDevices } from "./format";
import type { PushJobRow, SyncDevice, SyncEventsResponse, TopologyNode } from "./types";

export function TopologyOverview({
  topology,
  pollStatus,
  devices,
  pushJobs,
  selectedNode,
  onSelectNode,
}: {
  topology: SyncEventsResponse["topology"];
  pollStatus: SyncEventsResponse["poll_status"];
  devices: SyncDevice[];
  pushJobs?: PushJobRow[];
  selectedNode: TopologyNode | null;
  onSelectNode: (node: TopologyNode) => void;
}) {
  const isBackendSelected =
    selectedNode !== null && selectedNode.kind === "backend";

  const pendingByDevice = (deviceId: string) =>
    (pushJobs ?? []).filter(
      (pj) =>
        pj.status === "pending" &&
        pj.source_device_id !== deviceId
    ).length;

  return (
    <Card>
      <CardHeader>
        <CardTitle className="text-base flex items-center gap-2">
          <Wifi className="h-4 w-4" />
          Sync Topology
        </CardTitle>
        <CardDescription>
          Click a node to see details below.
        </CardDescription>
      </CardHeader>
      <CardContent className="space-y-3">
          {/* Backend node */}
            <button
              type="button"
              onClick={() => onSelectNode({ kind: "backend" })}
              className={`w-full rounded-lg border-2 p-4 text-left transition-colors ${
                isBackendSelected
                  ? "border-blue-500 bg-blue-500/5"
                  : "border-border hover:border-blue-300 bg-card"
              }`}
            >
              <div className="flex items-center gap-2 mb-2">
                <Server className="h-5 w-5 text-blue-500" />
                <span className="font-semibold text-sm">Backend</span>
              </div>
              {topology ? (
                <div className="flex flex-wrap gap-x-4 gap-y-1 text-xs text-muted-foreground">
                  <div>Rev: <span className="font-mono text-foreground">{topology.revision}</span></div>
                  <div>{topology.course_count} courses</div>
                  {topology.tombstone_count > 0 && (
                    <div className="text-orange-500">
                      {topology.tombstone_count} tombstones
                    </div>
                  )}
                  {topology.courses_reset_at && (
                    <div className="truncate" title={fmt(topology.courses_reset_at)}>
                      Reset: {relativeTime(topology.courses_reset_at)}
                    </div>
                  )}
                </div>
              ) : (
                <div className="text-xs text-muted-foreground">No topology data</div>
              )}
            </button>

          {/* Device nodes */}
          <div className="space-y-2">
            {sortedDevices(devices).map((device) => {
              const ps = pollStatus?.[device.id];
              const seenAgo = device.last_seen_at
                ? (Date.now() - new Date(device.last_seen_at).getTime()) / 1000
                : Infinity;
              const isOnline = ps ? ps === "foreground" : seenAgo < 30;
              const isMacOs = device.platform === "macos";
              const pending = pendingByDevice(device.id);
              const isSelected =
                selectedNode !== null &&
                selectedNode.kind === "device" &&
                selectedNode.device.id === device.id;

              return (
                <button
                  key={device.id}
                  type="button"
                  onClick={() => onSelectNode({ kind: "device", device })}
                  className={`w-full rounded-lg border-2 p-3 text-left transition-colors ${
                    isSelected
                      ? "border-blue-500 bg-blue-500/5"
                      : "border-border hover:border-blue-300 bg-card"
                  }`}
                >
                  <div className="flex items-center gap-2 flex-wrap">
                    {platformIcon(device.platform)}
                    <div className="min-w-0">
                      <div className="font-semibold text-sm truncate">
                        {platformLabel(device.platform)}
                      </div>
                    </div>
                    <span className="font-mono text-[10px] text-muted-foreground">
                      {device.client_device_id.slice(0, 12)}
                    </span>
                    {isOnline ? (
                      <Badge
                        variant="default"
                        className="bg-green-500/10 text-green-600 text-[10px] px-1.5 py-0"
                      >
                        <Wifi className="h-2.5 w-2.5 mr-0.5" />
                        Online
                      </Badge>
                    ) : (
                      <Badge variant="default" className="bg-gray-500/10 text-gray-500 text-[10px] px-1.5 py-0">
                        <WifiOff className="h-2.5 w-2.5 mr-0.5" />
                        Offline
                      </Badge>
                    )}
                    {device.cloud_sync_enabled === false && (
                      <Badge variant="secondary" className="text-[10px] px-1.5 py-0 gap-0.5 text-muted-foreground">
                        <CloudOff className="h-2.5 w-2.5" />device only
                      </Badge>
                    )}
                    {isMacOs && (
                      <Badge variant="outline" className="text-[10px] px-1.5 py-0 text-muted-foreground">
                        No push
                      </Badge>
                    )}
                    {pending > 0 && (
                      <Badge variant="outline" className="text-[10px] px-1.5 py-0 text-orange-500">
                        {pending} scheduled
                      </Badge>
                    )}
                    <span className="text-xs text-muted-foreground ml-auto">
                      {relativeTime(device.last_seen_at)}
                    </span>
                  </div>
                </button>
              );
            })}
            {devices.length === 0 && (
              <div className="text-sm text-muted-foreground py-4">
                No devices registered
              </div>
            )}
          </div>
      </CardContent>
    </Card>
  );
}
