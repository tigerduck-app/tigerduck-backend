// Presentation helpers shared by the Moodle tabs: timestamp formatting,
// device ordering and labelling, and the status badges.

import { Monitor, Tablet, Smartphone } from "lucide-react";
import { Badge } from "@/components/ui/badge";
import { PLATFORM_LABELS, platformLabel } from "@/lib/platform";
import type { SyncDevice } from "./types";

export function fmt(iso: string | null): string {
  if (!iso) return "—";
  return new Date(iso).toLocaleString();
}

export function relativeTime(iso: string | null): string {
  if (!iso) return "never";
  const diff = Date.now() - new Date(iso).getTime();
  if (diff < 0) return "just now";
  const secs = Math.floor(diff / 1000);
  if (secs < 60) return `${secs}s ago`;
  const mins = Math.floor(secs / 60);
  if (mins < 60) return `${mins}m ago`;
  const hrs = Math.floor(mins / 60);
  if (hrs < 24) return `${hrs}h ago`;
  return `${Math.floor(hrs / 24)}d ago`;
}

/** "in 3h 20m" for a time ahead, "due 5m ago" for one already passed. */
export function timeUntil(iso: string | null): string {
  if (!iso) return "—";
  const diff = new Date(iso).getTime() - Date.now();
  const mins = Math.floor(Math.abs(diff) / 60_000);
  const text =
    mins < 60 ? `${mins}m`
    : mins < 1440 ? `${Math.floor(mins / 60)}h ${mins % 60}m`
    : `${Math.floor(mins / 1440)}d ${Math.floor((mins % 1440) / 60)}h`;
  return diff < 0 ? `due ${text} ago` : `in ${text}`;
}

export function statusBadge(s: string) {
  switch (s) {
    case "pending": return <Badge variant="default">pending</Badge>;
    case "running": return <Badge className="bg-blue-600">running</Badge>;
    case "failed": return <Badge variant="destructive">failed</Badge>;
    case "disabled": return <Badge variant="outline">disabled</Badge>;
    default: return <Badge variant="secondary">{s}</Badge>;
  }
}

export function RunStatusBadge({ status }: { status: string }) {
  const colors: Record<string, string> = {
    succeeded: "bg-green-500/10 text-green-600",
    running: "bg-blue-500/10 text-blue-600",
    pending: "bg-yellow-500/10 text-yellow-600",
    failed: "bg-red-500/10 text-red-600",
    disabled: "bg-gray-500/10 text-gray-600",
    cancelled: "bg-gray-500/10 text-gray-600",
    ignored: "bg-gray-500/10 text-gray-600",
    locally_completed: "bg-green-500/10 text-green-600",
    none: "bg-gray-500/10 text-gray-600",
  };
  return (
    <Badge variant="default" className={colors[status] ?? "bg-gray-500/10 text-gray-600"}>
      {status}
    </Badge>
  );
}

export const CHART_COLORS = [
  "#3b82f6", "#ef4444", "#22c55e", "#f59e0b", "#8b5cf6",
  "#ec4899", "#14b8a6", "#f97316", "#6366f1", "#84cc16",
];

// Platform naming is shared with the devices pages — see lib/platform.
// Re-exported here so the Moodle tabs can keep importing it from this module.
export { PLATFORM_LABELS, platformLabel };

export const PLATFORM_ORDER: Record<string, number> = {
  android: 0, wearos: 1, ios: 2, ipados: 3, macos: 4, watchos: 5, web: 6,
};

export function sortedDevices(devices: SyncDevice[]): SyncDevice[] {
  return [...devices].sort((a, b) => {
    const pa = PLATFORM_ORDER[a.platform] ?? 99;
    const pb = PLATFORM_ORDER[b.platform] ?? 99;
    if (pa !== pb) return pa - pb;
    return a.client_device_id.localeCompare(b.client_device_id);
  });
}

export function deviceLabel(deviceId: string | null | undefined, devices: SyncDevice[]): string {
  if (!deviceId) return "—";
  const d = devices.find(d => d.id === deviceId);
  if (!d) return deviceId.slice(0, 8);
  return `${platformLabel(d.platform)} ${d.client_device_id.slice(0, 8)}`;
}

export function platformIcon(platform: string) {
  if (["android", "wearos"].includes(platform))
    return <Tablet className="h-5 w-5" />;
  if (["ios", "ipados", "watchos"].includes(platform))
    return <Smartphone className="h-5 w-5" />;
  return <Monitor className="h-5 w-5" />;
}
