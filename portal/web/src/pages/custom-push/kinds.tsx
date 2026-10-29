import { Archive, Bell, type LucideIcon } from "lucide-react";
import type { Tone } from "@/components/page";
import type { CustomPushTargetClass } from "@/types/api";

// The design's outlines: lucide draws an Android phone and tablet the same as an iPhone.
export const CLASSES: { id: CustomPushTargetClass; label: string; d: string }[] = [
  { id: "iphone", label: "iPhone", d: "M7 2h10a2 2 0 0 1 2 2v16a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2zM12 18h.01" },
  { id: "ipad", label: "iPad", d: "M5 2h14a2 2 0 0 1 2 2v16a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2zM12 18h.01" },
  { id: "mac", label: "Mac", d: "M4 4h16a1 1 0 0 1 1 1v11H3V5a1 1 0 0 1 1-1zM1 20h22" },
  { id: "android", label: "Android", d: "M7 2h10a2 2 0 0 1 2 2v16a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2zM10 5h4" },
  { id: "android_tablet", label: "Android tablet", d: "M3 5h18a2 2 0 0 1 2 2v10a2 2 0 0 1-2 2H3a2 2 0 0 1-2-2V7a2 2 0 0 1 2-2zM11 15h2" },
];

export const ALL_CLASSES = CLASSES.map((c) => c.id);

export function ClassIcon({ d, title, className }: { d: string; title?: string; className?: string }) {
  return (
    <svg
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth={2}
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden={title ? undefined : true}
      className={className ?? "size-4 shrink-0"}
    >
      {title && <title>{title}</title>}
      <path d={d} />
    </svg>
  );
}

export const KINDS: Record<"record" | "popup", { label: string; note: string; icon: LucideIcon; tone: Tone }> = {
  record: { label: "Record", note: "Also in Announcement", icon: Archive, tone: "info" },
  popup: { label: "Popup", note: "Notification only", icon: Bell, tone: "violet" },
};

/** "1 device", "1,284 devices" */
export function count(n: number, word: string): string {
  return `${n.toLocaleString("en-US")} ${word}${n === 1 ? "" : "s"}`;
}
