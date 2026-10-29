// A student about to be deregistered, or just deregistered: their devices
// and the rows that go with the account.

import { useState, type ReactNode } from "react";
import { CHART } from "@/lib/chart";
import { ago, stamp } from "@/lib/format";
import { familyLabel } from "@/lib/platform";
import { familyColor, osName, platformIcon } from "@/pages/devices/format";

export type ReportDevice = {
  device_id: string;
  client_device_id: string;
  platform: string;
  app_version: string | null;
  os_version: string | null;
  last_seen_at: string | null;
};

export type Snapshot = {
  student_id: string;
  account_created_at: string | null;
  devices: ReportDevice[];
  /** A table missing from the database reads "table_not_found". */
  deleted_counts: Record<string, number | string>;
};

const GROUPS: [string, string[]][] = [
  ["Sync", ["sync_log_entries", "sync_jobs", "user_sync_state"]],
  ["Bulletins", ["user_bulletin_states", "user_bulletin_subscriptions"]],
  ["Push", ["push_jobs", "device_push_tokens"]],
  ["Assignments", ["user_assignments", "user_assignment_overrides"]],
  ["Courses", ["user_courses", "user_course_overrides", "user_course_skipped_dates"]],
  ["Sessions", ["auth_sessions"]],
  [
    "Settings and school account",
    ["user_settings_documents", "external_accounts", "external_account_credentials"],
  ],
];

export const fmt = (n: number) => n.toLocaleString("en-US");
const count = (v: number | string | undefined) => (typeof v === "number" ? v : 0);

export function totalRows(s: Snapshot): number {
  return Object.values(s.deleted_counts).reduce<number>((sum, v) => sum + count(v), 0);
}

export function Initial({ name, id, className }: { name: string | null; id: string; className: string }) {
  return (
    <span
      aria-hidden="true"
      className={`flex shrink-0 items-center justify-center rounded-full font-semibold ${className}`}
    >
      {(name || id).charAt(0)}
    </span>
  );
}

export function StudentCard({
  snapshot,
  name,
  lastActive,
  report,
  footer,
}: {
  snapshot: Snapshot;
  name: string | null;
  lastActive: string | null;
  report: boolean;
  footer?: ReactNode;
}) {
  const [hover, setHover] = useState<number | null>(null);
  const groups = GROUPS.map(([label, tables], i) => ({
    label,
    color: CHART[i],
    tables,
    sum: tables.reduce((s, t) => s + count(snapshot.deleted_counts[t]), 0),
  }));
  const total = totalRows(snapshot);
  const shown = groups.filter((g) => g.sum > 0);
  let left = 0;
  const mids = shown.map((g) => {
    const mid = left + g.sum / total / 2;
    left += g.sum / total;
    return mid;
  });
  const hovered = hover === null ? null : shown[hover];
  const line = [
    snapshot.account_created_at && `Joined ${stamp(snapshot.account_created_at).slice(0, 10)}`,
    lastActive && `last active ${ago(lastActive)}`,
  ]
    .filter(Boolean)
    .join(" · ");

  return (
    <section aria-labelledby="who-h" className="min-w-0 overflow-hidden rounded-[14px] border border-line bg-surface">
      <div className="flex flex-wrap items-center gap-4 border-b border-line px-6 py-5 phone:px-4">
        <Initial name={name} id={snapshot.student_id} className="size-12 bg-soft text-lg" />
        <div className="flex min-w-0 flex-col gap-0.5">
          <h2 id="who-h" className="flex flex-wrap items-baseline gap-x-2.5">
            <span className="font-mono text-lg font-medium">{snapshot.student_id}</span>
            {name && <span className="text-base font-medium">{name}</span>}
          </h2>
          {line && <span className="text-[13px] tabular-nums text-ink-3">{line}</span>}
        </div>
      </div>

      {/* The 1px gap draws the divider, beside or below whichever way the columns wrap. */}
      <div className="flex flex-wrap gap-px bg-divider">
        <div className="flex min-w-0 flex-[1_1_340px] flex-col gap-2.5 bg-surface px-6 py-[18px] phone:px-4">
          <div className="flex items-baseline justify-between">
            <h3 className="text-[13px] font-semibold">Devices</h3>
            <span className="text-xs tabular-nums text-ink-3">{snapshot.devices.length}</span>
          </div>
          <ul className="flex flex-col gap-2">
            {snapshot.devices.map((d) => {
              const Icon = platformIcon(d.platform);
              return (
                <li key={d.device_id} className="flex items-center gap-3 rounded-[10px] bg-ground px-3 py-2.5">
                  <Icon
                    aria-hidden="true"
                    className="h-[18px] w-[18px] shrink-0"
                    style={{ color: familyColor(familyLabel(d.platform)) }}
                  />
                  <div className="flex min-w-0 flex-1 flex-col">
                    <span className="text-[13px] font-medium">
                      {osName(d)}
                      {d.app_version && (
                        <span className="font-mono font-normal text-ink-3"> · app {d.app_version}</span>
                      )}
                    </span>
                    <span title={d.client_device_id} className="truncate font-mono text-xs text-ink-3">
                      {d.client_device_id}
                    </span>
                  </div>
                  <span className="whitespace-nowrap text-xs text-ink-3">
                    {d.last_seen_at ? ago(d.last_seen_at) : "—"}
                  </span>
                </li>
              );
            })}
          </ul>
        </div>

        <div className="flex min-w-0 flex-[2_1_520px] flex-col gap-3.5 bg-surface px-6 py-[18px] phone:px-4">
          <div className="flex items-baseline justify-between">
            <h3 className="text-[13px] font-semibold">
              {report ? "Deleted rows" : "Deleted with the account"}
            </h3>
            <span className="text-xs tabular-nums text-ink-3">{fmt(total)} rows</span>
          </div>
          {shown.length > 0 && (
            <div className="relative">
              <div
                role="img"
                aria-label={groups.map((g) => `${g.label} ${g.sum}`).join(", ")}
                className="flex h-3.5 gap-0.5"
              >
                {shown.map((g, i) => (
                  <div
                    key={g.label}
                    onMouseEnter={() => setHover(i)}
                    onMouseLeave={() => setHover(null)}
                    className="h-3.5 min-w-1 first:rounded-l last:rounded-r"
                    style={{ flex: `${g.sum} 1 0`, background: g.color }}
                  />
                ))}
              </div>
              {hovered && (
                <div
                  role="tooltip"
                  style={{ left: `${mids[hover!] * 100}%` }}
                  className="pointer-events-none absolute bottom-[22px] z-10 -translate-x-1/2 whitespace-nowrap rounded-lg bg-[#15171a] px-2 py-1.5 text-xs text-white"
                >
                  {hovered.label}: {fmt(hovered.sum)} rows ({((hovered.sum / total) * 100).toFixed(1)}%)
                </div>
              )}
            </div>
          )}
          <div className="grid grid-cols-[repeat(auto-fill,minmax(190px,1fr))] gap-2 phone:grid-cols-2">
            {groups.map((g) => (
              <div key={g.label} className="flex min-w-0 flex-col gap-1.5 rounded-xl border border-line px-3.5 py-3">
                <div className="flex items-center gap-2">
                  <span aria-hidden="true" className="size-2.5 shrink-0 rounded-[3px]" style={{ background: g.color }} />
                  <span className="min-w-0 text-[13px] font-medium">{g.label}</span>
                  <span className="ml-auto text-base font-semibold tabular-nums">{fmt(g.sum)}</span>
                </div>
                {g.tables.map((t) => {
                  const v = snapshot.deleted_counts[t];
                  return (
                    <div key={t} className="flex justify-between gap-2 text-xs phone:hidden">
                      <span className="truncate font-mono text-ink-3">{t}</span>
                      <span className={count(v) ? "tabular-nums" : "tabular-nums text-side-muted"}>
                        {typeof v === "number" ? fmt(v) : "—"}
                      </span>
                    </div>
                  );
                })}
              </div>
            ))}
          </div>
        </div>
      </div>
      {footer && (
        <div className="flex flex-wrap justify-end gap-2 border-t border-line px-6 py-3.5 phone:px-4">
          {footer}
        </div>
      )}
    </section>
  );
}
