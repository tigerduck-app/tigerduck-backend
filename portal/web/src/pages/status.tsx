// Status: who uses the app and how, from /api/users/stats, and the user list.
// The stack's own health is on Stack; the header only says whether it is up.

import * as React from "react";
import { useQuery } from "@tanstack/react-query";
import { Link } from "react-router-dom";
import { Check, TriangleAlert, X } from "lucide-react";
import { api } from "@/lib/api";
import { cn } from "@/lib/cn";
import { monthDay } from "@/lib/format";
import { platformLabel } from "@/lib/platform";
import { Skeleton } from "@/components/ui/skeleton";
import { Dot, ErrorAlert, PageHeading } from "@/components/page";
import type { AccountState, StatusPayload, UserStats } from "@/types/api";
import { ColumnChart, Spark } from "./status/charts";
import { ACCOUNT, num } from "./status/format";
import { Card } from "./status/Card";
import { UsersPanel } from "./status/UsersPanel";

const RANGES = [7, 30, 90];
const VERSION_COLORS = ["#2a78d6", "#eb6834", "#1baf7a", "#eda100", "#e87ba4", "#008300", "#6250d6"];
const OLDER = "Older";
const OLDER_COLOR = "#9AA0A8";
const FEATURES: [keyof UserStats["devices"]["features"], string][] = [
  ["class_reminders", "Class reminders"],
  ["bulletin_push", "Bulletin pushes"],
  ["live_activity", "Live Activities"],
  ["assignment_reminders", "Assignment reminders"],
  ["cloud_sync", "Cloud sync"],
  ["course_name_sync", "Course name sync"],
];
const CHANNELS: [string, string][] = [
  ["schedule", "Live Activities"],
  ["course", "Class reminders"],
  ["bulletin", "Bulletins"],
  ["assignment", "Assignment reminders"],
  ["custom", "Custom pushes"],
  ["system", "System"],
];
const ACCOUNT_ICON: Record<AccountState, typeof Check> = {
  signed_in: Check,
  needs_sign_in: TriangleAlert,
  failing: X,
};
const ACCOUNT_COLOR: Record<AccountState, string> = {
  signed_in: "bg-dot-ok",
  needs_sign_in: "bg-dot-warn",
  failing: "bg-dot-bad",
};
const ACCOUNT_TEXT: Record<AccountState, string> = {
  signed_in: "text-dot-ok",
  needs_sign_in: "text-dot-warn",
  failing: "text-dot-bad",
};

const day = (iso: string) => monthDay(iso, true);

export function StatusPage() {
  const stats = useQuery<UserStats>({
    queryKey: ["user-stats"],
    queryFn: () => api<UserStats>("/api/users/stats"),
    refetchInterval: 60_000,
  });
  const stack = useQuery<StatusPayload>({
    queryKey: ["status"],
    queryFn: () => api<StatusPayload>("/api/status"),
    refetchInterval: 60_000,
  });
  const down = stack.data?.containers.filter((c) => c.state !== "running").length ?? 0;

  return (
    <>
      <PageHeading
        group="Overview"
        title="Status"
        actions={
          stats.dataUpdatedAt > 0 && (
            <span className="flex items-center gap-2 text-[13px] text-ink-3">
              {stack.data && <Dot tone={down ? "warn" : "ok"} />}
              {stack.data &&
                (down ? (
                  <Link to="/stack">{down} not running</Link>
                ) : (
                  "All systems running"
                ))}
              {stack.data && " · "}
              updated{" "}
              {new Date(stats.dataUpdatedAt).toLocaleTimeString("en-GB", {
                hour: "2-digit",
                minute: "2-digit",
              })}
            </span>
          )
        }
      />
      {stats.isLoading && <Skeleton className="h-[480px] w-full rounded-[14px]" />}
      {stats.isError && (
        <ErrorAlert className="items-center" onRetry={() => stats.refetch()}>
          Failed to load status: {(stats.error as Error).message}
        </ErrorAlert>
      )}
      {stats.data && <Overview stats={stats.data} />}
      <UsersPanel />
    </>
  );
}

function Overview({ stats }: { stats: UserStats }) {
  const [range, setRange] = React.useState(30);
  const { users, active, devices, pushes } = stats;
  const deviceTotal = devices.versions.reduce((s, v) => s + v.devices, 0);
  const byPlatform = new Map<string, number>();
  for (const v of devices.versions) {
    byPlatform.set(v.platform, (byPlatform.get(v.platform) ?? 0) + v.devices);
  }
  const platformNote = [...byPlatform]
    .sort((a, b) => b[1] - a[1])
    .map(([p, n]) => `${platformLabel(p)} ${num(n)}`)
    .join(" · ");
  const today = pushes.daily[pushes.daily.length - 1] ?? { sent: 0, failed: 0 };
  const todayTotal = today.sent + today.failed;
  // Users at the end of each week: today's count less those who joined later.
  const cumulative = users.weekly.map(
    (_, i) => users.total - users.weekly.slice(i + 1).reduce((s, w) => s + w.users, 0),
  );
  const tiles = [
    {
      label: "Users",
      value: num(users.total),
      note: `+${num(users.new_7d)} this week`,
      spark: cumulative,
    },
    {
      label: "Active, last 7 days",
      value: num(active.last_7d),
      note: `${users.total ? Math.round((active.last_7d / users.total) * 100) : 0}% of users`,
      spark: active.daily.slice(-20).map((d) => d.users),
    },
    {
      label: "Signed-in devices",
      value: num(deviceTotal),
      note: platformNote || "—",
      spark: devices.daily.map((d) => d.devices),
    },
    { label: "Anonymous devices", value: num(stats.anonymous_devices), note: "Bulletins only" },
    {
      label: "Pushes today",
      value: num(todayTotal),
      note: `${todayTotal ? ((today.failed / todayTotal) * 100).toFixed(1) : "0"}% failed`,
      spark: pushes.daily.map((d) => d.sent + d.failed),
    },
  ];

  const series = active.daily.slice(-range);
  const values = series.map((d) => d.users);

  return (
    <>
      <div className="grid grid-cols-[repeat(auto-fit,minmax(200px,1fr))] gap-3 phone:grid-cols-2">
        {tiles.map((t) => (
          <div
            key={t.label}
            className="flex min-w-0 flex-col gap-1 rounded-[14px] border border-line bg-surface px-[18px] pb-3.5 pt-4"
          >
            <span className="text-xs font-medium text-ink-3">{t.label}</span>
            <span className="text-[26px] font-semibold tabular-nums tracking-[-0.01em]">{t.value}</span>
            <span title={t.note} className="truncate text-xs text-ink-2">
              {t.note}
            </span>
            {t.spark && <Spark values={t.spark} />}
          </div>
        ))}
      </div>

      <div className="flex flex-wrap items-stretch gap-6">
        <Card
          title="Daily active users"
          className="flex-[2_1_560px]"
          aside={
            <div role="radiogroup" aria-label="Range" className="flex gap-1 rounded-[10px] bg-soft p-1">
              {RANGES.map((r) => (
                <button
                  key={r}
                  type="button"
                  role="radio"
                  aria-checked={range === r}
                  onClick={() => setRange(r)}
                  className={cn(
                    "min-h-[30px] rounded-[7px] px-3 text-[13px]",
                    range === r
                      ? "bg-surface font-semibold text-ink shadow-[0_1px_2px_rgba(20,22,26,0.12)]"
                      : "text-ink-2",
                  )}
                >
                  {r} days
                </button>
              ))}
            </div>
          }
        >
          <ColumnChart
            axis
            className="gap-0.5"
            points={series.map((d) => ({ label: day(d.day), value: d.users }))}
            aria={`Daily active users, last ${range} days, between ${Math.min(...values)} and ${Math.max(...values)}`}
            tip={(p) => [`${num(p.value)} users`, p.label]}
          />
        </Card>
        <Card title="New users per week" className="flex-[1_1_320px] phone:hidden">
          <ColumnChart
            className="gap-1"
            points={users.weekly.map((w) => ({ label: day(w.week), value: w.users }))}
            aria={`New users per week for ${users.weekly.length} weeks`}
            tip={(p) => [`${num(p.value)} new`, `week of ${p.label}`]}
          />
        </Card>
      </div>

      <div className="flex flex-wrap items-stretch gap-6">
        <Versions versions={devices.versions} />
        <Card title="Turned on, share of signed-in devices" className="flex-[1_1_400px]">
          <div className="flex flex-col gap-2.5">
            {FEATURES.map(([key, label]) => {
              const n = devices.features[key];
              const perUser = key === "class_reminders";
              const base = perUser ? devices.users : deviceTotal;
              const pct = base ? Math.round((n / base) * 100) : 0;
              const of = `${num(n)} of ${num(base)} ${perUser ? "users" : "devices"}`;
              return (
                <div key={key} title={`${label}: ${of}`} className="flex items-center gap-3">
                  <span className="w-[150px] shrink-0 text-[13px] phone:w-[104px]">{label}</span>
                  <div aria-hidden="true" className="h-2.5 flex-1 rounded-full bg-divider">
                    <div className="h-2.5 rounded-full bg-chart-1" style={{ width: `${pct}%` }} />
                  </div>
                  <span className="w-10 shrink-0 text-right text-[13px] font-medium tabular-nums">{pct}%</span>
                </div>
              );
            })}
          </div>
        </Card>
      </div>

      <div className="flex flex-wrap items-stretch gap-6">
        <Accounts accounts={stats.accounts} />
        <Card
          title="Pushes in the last 24 hours"
          className="flex-[1_1_520px] gap-3"
          aside={<span className="text-xs text-ink-3">sent · failed</span>}
        >
          <Channels channels={pushes.channels} />
        </Card>
      </div>
    </>
  );
}

function compareVersions(a: string, b: string): number {
  const x = a.split(".");
  const y = b.split(".");
  for (let i = 0; i < Math.max(x.length, y.length); i++) {
    const d = (Number(x[i]) || 0) - (Number(y[i]) || 0);
    if (d) return d;
  }
  return a.localeCompare(b);
}

// Each platform's three newest versions; the rest, and devices that never
// reported one, count as Older.
function Versions({ versions }: { versions: UserStats["devices"]["versions"] }) {
  const [hover, setHover] = React.useState<string | null>(null);
  const rows = [...new Set(versions.map((v) => v.platform))].map((platform) => {
    const own = versions.filter((v) => v.platform === platform && v.app_version);
    const kept = own.sort((a, b) => compareVersions(b.app_version, a.app_version)).slice(0, 3);
    const total = versions.filter((v) => v.platform === platform).reduce((s, v) => s + v.devices, 0);
    const older = total - kept.reduce((s, v) => s + v.devices, 0);
    const segs = kept.map((v) => ({ version: v.app_version, devices: v.devices }));
    return { platform, total, segs: older ? [...segs, { version: OLDER, devices: older }] : segs };
  });
  rows.sort((a, b) => b.total - a.total);
  const legend = [...new Set(rows.flatMap((r) => r.segs.map((s) => s.version)).filter((v) => v !== OLDER))]
    .sort((a, b) => compareVersions(b, a))
    .slice(0, VERSION_COLORS.length);
  const hasOlder = rows.some((r) => r.segs.some((s) => s.version === OLDER));
  const color = (v: string) => (v === OLDER ? OLDER_COLOR : VERSION_COLORS[legend.indexOf(v)] ?? OLDER_COLOR);
  const max = Math.max(1, ...rows.map((r) => r.total));
  let tip = "";

  return (
    <Card
      title="Signed-in devices by app version"
      className="flex-[1_1_520px] phone:hidden"
      aside={
        <div className="flex flex-wrap gap-x-3.5 gap-y-1.5 text-xs text-ink-2">
          {[...legend, ...(hasOlder ? [OLDER] : [])].map((v) => (
            <span key={v} className="flex items-center gap-1.5">
              <span aria-hidden="true" className="size-3 rounded-[3px]" style={{ background: color(v) }} />
              {v}
            </span>
          ))}
        </div>
      }
    >
      <div className="flex flex-col gap-3.5">
        {rows.map((r) => (
          <div key={r.platform} className="flex items-center gap-3">
            <span className="w-[72px] shrink-0 text-[13px] font-medium">{platformLabel(r.platform)}</span>
            <div className="relative flex h-[26px] min-w-0 flex-1 gap-0.5">
              {r.segs.map((s, i) => {
                const key = `${r.platform} ${s.version}`;
                const pct = Math.round((s.devices / r.total) * 100);
                const width = (s.devices / max) * 100;
                if (hover === key) tip = `${platformLabel(r.platform)} ${s.version}: ${num(s.devices)} devices (${pct}%)`;
                return (
                  <span
                    key={s.version}
                    title={`${platformLabel(r.platform)} ${s.version}: ${s.devices}`}
                    onMouseEnter={() => setHover(key)}
                    onMouseLeave={() => setHover(null)}
                    className={cn(
                      "flex h-full shrink-0 items-center justify-center overflow-hidden text-[11px] font-semibold text-white",
                      i === 0 && "rounded-l-[4px]",
                      i === r.segs.length - 1 && "rounded-r-[4px]",
                      hover === key && "shadow-[inset_0_0_0_2px_#15171A]",
                    )}
                    style={{ width: `calc(${width.toFixed(2)}% - 2px)`, background: color(s.version) }}
                  >
                    {width > 7 ? `${pct}%` : ""}
                  </span>
                );
              })}
            </div>
            <span className="w-[52px] shrink-0 text-right text-[13px] tabular-nums text-ink-2">{num(r.total)}</span>
          </div>
        ))}
        {tip && (
          <div className="self-start rounded-lg bg-[#15171A] px-2.5 py-1.5 text-xs font-semibold text-white">{tip}</div>
        )}
      </div>
    </Card>
  );
}

function Accounts({ accounts }: { accounts: UserStats["accounts"] }) {
  const states = Object.keys(ACCOUNT) as AccountState[];
  const total = states.reduce((s, k) => s + accounts[k], 0);
  const shown = states.filter((k) => accounts[k] > 0);
  return (
    <Card title="School accounts" className="flex-[1_1_400px]">
      <div
        role="img"
        aria-label={states.map((k) => `${ACCOUNT[k].label} ${accounts[k]}`).join(", ")}
        className="flex h-[26px] gap-0.5 rounded-[4px] bg-divider"
      >
        {shown.map((k, i) => (
          <span
            key={k}
            className={cn(
              "h-full",
              ACCOUNT_COLOR[k],
              i === 0 && "rounded-l-[4px]",
              i === shown.length - 1 && "rounded-r-[4px]",
            )}
            style={{ width: `calc(${((accounts[k] / total) * 100).toFixed(2)}% - 2px)` }}
          />
        ))}
      </div>
      <div className="flex flex-wrap gap-x-6 gap-y-2.5">
        {states.map((k) => {
          const Icon = ACCOUNT_ICON[k];
          return (
            <div key={k} className="flex items-center gap-2 text-[13px]">
              <Icon className={cn("h-4 w-4", ACCOUNT_TEXT[k])} strokeWidth={2.2} />
              {ACCOUNT[k].label}
              <span className="font-semibold tabular-nums">{num(accounts[k])}</span>
            </div>
          );
        })}
      </div>
    </Card>
  );
}

// Every channel, sent or not, in a fixed order; one the page does not know goes last.
function Channels({ channels }: { channels: UserStats["pushes"]["channels"] }) {
  const known = new Set(CHANNELS.map(([c]) => c));
  const rows = [
    ...CHANNELS.map(([key, label]) => {
      const c = channels.find((x) => x.channel === key);
      return { key, label, sent: c?.sent ?? 0, failed: c?.failed ?? 0 };
    }),
    ...channels.filter((c) => !known.has(c.channel)).map((c) => ({ key: c.channel, label: c.channel, ...c })),
  ];
  const max = Math.max(1, ...rows.map((r) => r.sent));
  return (
    <div className="flex flex-col gap-2">
      {rows.map((r) => (
        <div
          key={r.key}
          title={`${r.label}: ${num(r.sent)} sent, ${num(r.failed)} failed`}
          className="flex items-center gap-3"
        >
          <span className="w-[150px] shrink-0 text-[13px] phone:w-[104px]">{r.label}</span>
          <div aria-hidden="true" className="h-3.5 flex-1">
            <div
              className="h-3.5 rounded-r-[4px] bg-chart-1"
              style={{ width: `${Math.max(1, (r.sent / max) * 100).toFixed(1)}%` }}
            />
          </div>
          <span className="w-14 shrink-0 text-right text-[13px] font-medium tabular-nums">{num(r.sent)}</span>
          <span
            className={cn(
              "w-10 shrink-0 text-right text-[13px] tabular-nums",
              r.failed ? "text-badge-danger-fg" : "text-ink-3",
            )}
          >
            {num(r.failed)}
          </span>
        </div>
      ))}
    </div>
  );
}
