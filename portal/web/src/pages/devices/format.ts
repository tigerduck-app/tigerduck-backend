// How the device pages name and group a device.

import { Laptop, Smartphone, Tablet, type LucideIcon } from "lucide-react";
import { CHART, CHART_OTHER } from "@/lib/chart";
import { familyLabel, platformLabel } from "@/lib/platform";
import type { DeviceRow } from "@/types/api";

export const PLATFORMS = ["iPhone", "iPad", "macOS", "Android"] as const;

const PLATFORM_NAME: Record<string, string> = {
  ios: "iPhone",
  ipados: "iPad",
  macos: "macOS",
  android: "Android",
};

export function platformName(platform: string): string {
  return PLATFORM_NAME[platform] ?? platformLabel(platform);
}

export function platformIcon(platform: string): LucideIcon {
  if (platform === "ipados") return Tablet;
  if (platform === "macos") return Laptop;
  return Smartphone;
}

/** Apple blue, Android orange, anything else grey. */
export function familyColor(family: string): string {
  return family === "Apple" ? CHART[0] : family === "Android" ? CHART[1] : CHART_OTHER;
}

export function osName(d: Pick<DeviceRow, "platform" | "os_version">): string {
  return d.os_version ? `${platformLabel(d.platform)} ${d.os_version}` : "—";
}

/** The two apps version independently, so a version is named with its build family. */
export function appRelease(d: DeviceRow): string {
  return `${familyLabel(d.platform)} ${d.app_version ?? "—"}`;
}

export function tokens(d: DeviceRow): string[] {
  return [
    d.has_device_token && "apns",
    d.has_pts_token && "pts",
    d.has_fcm_token && "fcm",
  ].filter((t): t is string => !!t);
}

export function tokensLabel(d: DeviceRow): string {
  return tokens(d).join(" + ") || "none";
}

const CLASS_LABEL: Record<string, string> = {
  iphone: "iPhone",
  ipad: "iPad",
  mac: "Mac",
  android: "Android",
  android_tablet: "Android tablet",
};

export function classLabel(deviceClass: string): string {
  return CLASS_LABEL[deviceClass] ?? (deviceClass || "—");
}
