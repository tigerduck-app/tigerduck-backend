// Tab definitions, platform labels, and formatting for the device pages.

import { ApiError } from "@/lib/api";
import type { DeviceRow } from "@/types/api";

// Sub-tab key → matcher on the device row. v3 reports platform
// `ios`/`ipados` (mapped to device_class iphone/ipad server-side);
// Android registers without an apns env so we gate on `platform`.
export const TABS: Array<{
  key: string;
  label: string;
  match: (d: DeviceRow) => boolean;
}> = [
  { key: "iphone", label: "iPhone", match: (d) => d.device_class === "iphone" },
  { key: "ipad", label: "iPad", match: (d) => d.device_class === "ipad" },
  { key: "macos", label: "macOS", match: (d) => d.platform === "macos" },
  { key: "android", label: "Android", match: (d) => d.platform === "android" },
];

export const PLATFORM_LABELS: Record<string, string> = {
  ios: "iOS",
  ipados: "iPadOS",
  macos: "macOS",
  watchos: "watchOS",
  android: "Android",
  wearos: "Wear OS",
  web: "Web",
};

export function platformLabel(p: string) {
  return PLATFORM_LABELS[p] ?? p;
}

export function formatTs(ts: string): string {
  try {
    return new Date(ts).toLocaleString();
  } catch {
    return ts;
  }
}

export function asMessage(e: unknown): string {
  if (e instanceof ApiError) return `HTTP ${e.status} — ${e.message}`;
  if (e instanceof Error) return e.message;
  return String(e);
}
