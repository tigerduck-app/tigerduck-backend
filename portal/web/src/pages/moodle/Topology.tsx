// The account as nodes: the backend's copy, and each signed-in device with
// what the server has queued for it. Picking a node opens its panel below.

import type { ReactNode } from "react";
import { Laptop, Server, Smartphone, Tablet } from "lucide-react";
import { Dot, Pill } from "@/components/page";
import { cn } from "@/lib/cn";
import { ago, deviceNoun } from "./format";
import type { QueuedJob, SyncDevice, SyncEventsResponse } from "./types";

export function DeviceIcon({ platform, className }: { platform: string; className?: string }) {
  const Icon = platform === "macos" ? Laptop : platform === "ipados" ? Tablet : Smartphone;
  return <Icon className={className ?? "h-4 w-4"} />;
}

/** Seen within the last 30 s. */
export const isOnline = (d: SyncDevice) =>
  !!d.last_seen_at && Date.now() - Date.parse(d.last_seen_at) < 30_000;

export const takesPush = (d: SyncDevice) => d.platform !== "macos" && d.has_push_token;

export const queuedFor = (jobs: QueuedJob[], deviceId: string) =>
  jobs.filter((j) => j.recipients.some((r) => r.device_id === deviceId));

/** The badges a device carries in both the topology and its panel. */
export function deviceBadges(d: SyncDevice, queued: number): ReactNode[] {
  return [
    d.cloud_sync_enabled === false && <Pill key="local">Device only</Pill>,
    !takesPush(d) && <Pill key="push">No push</Pill>,
    queued > 0 && (
      <Pill key="queued" tone="orange">
        {queued} scheduled
      </Pill>
    ),
  ].filter(Boolean);
}

const node = (on: boolean) =>
  cn(
    "flex min-w-0 flex-col items-start gap-2 rounded-xl border-2 px-3.5 py-3 text-left",
    on ? "border-link bg-selected" : "border-line bg-surface hover:border-field",
  );

export function Topology({
  data,
  devices,
  selected,
  onSelect,
}: {
  data: SyncEventsResponse;
  devices: SyncDevice[];
  selected: string;
  onSelect: (node: string) => void;
}) {
  const t = data.topology;
  const queued = data.queued_jobs ?? [];
  return (
    <section
      aria-labelledby="topo-h"
      className="flex flex-col gap-3 rounded-[14px] border border-line bg-surface px-6 pb-5 pt-4 phone:px-4"
    >
      <div className="flex items-baseline gap-2.5">
        <h2 id="topo-h" className="text-[15px] font-semibold">
          Topology
        </h2>
        <span className="text-xs text-ink-3">
          Backend and {devices.length} {devices.length === 1 ? "device" : "devices"}
        </span>
      </div>
      <div className="flex items-stretch tablet:flex-col tablet:gap-3">
        <button
          type="button"
          aria-pressed={selected === "backend"}
          onClick={() => onSelect("backend")}
          className={cn(node(selected === "backend"), "flex-[1_1_0]")}
        >
          <span className="flex w-full min-w-0 items-center gap-2">
            <span
              aria-hidden="true"
              className="flex size-[26px] shrink-0 items-center justify-center rounded-[7px] bg-badge-info text-badge-info-fg"
            >
              <Server className="h-3.5 w-3.5" />
            </span>
            <span className="whitespace-nowrap font-semibold">Backend</span>
            {t && <span className="ml-auto shrink-0 font-mono text-xs text-ink-2">rev {t.revision}</span>}
          </span>
          {t && (
            <span className="flex flex-wrap gap-1">
              <Pill className="px-[7px] py-px text-[11px]">{t.course_count} courses</Pill>
              {t.tombstone_count > 0 && (
                <Pill tone="orange" className="px-[7px] py-px text-[11px]">
                  {t.tombstone_count} deleted
                </Pill>
              )}
            </span>
          )}
        </button>
        {devices.length > 0 && (
          <span aria-hidden="true" className="h-0.5 flex-[0_0_28px] self-center bg-disabled tablet:hidden" />
        )}
        {devices.length > 0 && (
          <div className="flex min-w-0 flex-[3_1_0] flex-wrap gap-3 phone:flex-col">
            {devices.map((d) => {
              const online = isOnline(d);
              return (
                <button
                  key={d.id}
                  type="button"
                  aria-pressed={selected === d.id}
                  onClick={() => onSelect(d.id)}
                  className={cn(node(selected === d.id), "flex-[1_1_180px] phone:basis-auto")}
                >
                  <span className="flex w-full min-w-0 items-center gap-2">
                    <DeviceIcon platform={d.platform} className="h-4 w-4 shrink-0" />
                    <span className="whitespace-nowrap font-semibold">{deviceNoun(d.platform)}</span>
                    <span title={d.client_device_id} className="min-w-0 truncate font-mono text-[11px] text-ink-3">
                      {d.client_device_id.slice(0, 8)}
                    </span>
                    <span className="ml-auto inline-flex shrink-0 items-center gap-[5px] whitespace-nowrap text-xs text-ink-2">
                      <Dot tone={online ? "ok" : "off"} className="size-[7px]" />
                      {online ? "Online" : ago(d.last_seen_at)}
                    </span>
                  </span>
                  <span className="flex flex-wrap gap-1 [&>span]:px-[7px] [&>span]:py-px [&>span]:text-[11px]">
                    {deviceBadges(d, queuedFor(queued, d.id).length)}
                  </span>
                </button>
              );
            })}
          </div>
        )}
      </div>
    </section>
  );
}
