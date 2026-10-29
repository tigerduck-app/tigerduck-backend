// Labels shared by the Status page and the user page.

import { Globe, Laptop, Monitor, Smartphone, Tablet, Watch, type LucideIcon } from "lucide-react";
import type { DotTone } from "@/components/page";
import type { AccountState } from "@/types/api";

export const ACCOUNT: Record<AccountState, { label: string; tone: DotTone }> = {
  signed_in: { label: "Signed in", tone: "ok" },
  needs_sign_in: { label: "Needs sign-in", tone: "warn" },
  failing: { label: "Failing", tone: "bad" },
};

export const PLATFORM_ICON: Record<string, LucideIcon> = {
  ios: Smartphone,
  android: Smartphone,
  ipados: Tablet,
  macos: Laptop,
  watchos: Watch,
  wearos: Watch,
  windows: Monitor,
  web: Globe,
};

export const num = (n: number) => n.toLocaleString("en-US");

/** "12 min ago", "3 h ago", "2 d ago" */
export function ago(iso: string, now = Date.now()): string {
  const minutes = Math.max(0, Math.floor((now - Date.parse(iso)) / 60_000));
  if (minutes < 60) return `${minutes} min ago`;
  if (minutes < 1440) return `${Math.floor(minutes / 60)} h ago`;
  return `${Math.floor(minutes / 1440)} d ago`;
}

/** "1151" -> "115-1", as the apps show a term code. */
export const termLabel = (code: string) => `${code.slice(0, 3)}-${code.slice(3)}`;
