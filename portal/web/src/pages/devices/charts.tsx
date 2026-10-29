// The Statistics view: KPI tiles and horizontal bar charts of the devices.

import { useState, type ReactNode } from "react";
import { CHART, CHART_OTHER } from "@/lib/chart";
import { familyLabel } from "@/lib/platform";
import type { DeviceRow } from "@/types/api";
import { Segmented } from "./Segmented";
import { familyColor, osName, platformName, tokens } from "./format";

type Bar = { label: string; value: number; color: string };

const SCOPES = [
  ["all", "All platforms"],
  ["apple", "All Apple"],
  ["iPhone", "iPhone"],
  ["iPad", "iPad"],
  ["macOS", "macOS"],
  ["Android", "Android"],
] as const;
type Scope = (typeof SCOPES)[number][0];

const WEEK_MS = 7 * 86_400_000;

function countBy(list: DeviceRow[], key: (d: DeviceRow) => string): [string, number][] {
  const counts = new Map<string, number>();
  for (const d of list) counts.set(key(d), (counts.get(key(d)) ?? 0) + 1);
  return [...counts].sort((a, b) => b[1] - a[1] || a[0].localeCompare(b[0]));
}

const pct = (part: number, total: number) => (total ? `${Math.round((part / total) * 100)}%` : "—");

function Bars({ bars, total }: { bars: Bar[]; total: number }) {
  const max = Math.max(1, ...bars.map((b) => b.value));
  return bars.map((b) => {
    const width = Math.max((b.value / max) * 100, 1.5);
    return (
      <div
        key={b.label}
        tabIndex={0}
        className="group grid min-h-7 grid-cols-[132px_minmax(0,1fr)_88px] items-center gap-3 outline-none phone:grid-cols-[96px_minmax(0,1fr)_64px] phone:gap-2"
      >
        <span title={b.label} className="truncate text-[13px]">
          {b.label}
        </span>
        <div className="relative flex h-[18px] items-center">
          <div
            className="h-[18px] rounded-r-[4px]"
            style={{ width: `${width}%`, background: b.color }}
          />
          <div
            role="tooltip"
            style={{ left: `${width}%` }}
            className="pointer-events-none absolute bottom-full z-10 mb-1 hidden -translate-x-1/2 whitespace-nowrap rounded-lg bg-[#15171a] px-2 py-1.5 text-xs text-white group-hover:block group-focus-visible:block"
          >
            {b.label}: {b.value} of {total} devices ({((b.value / total) * 100).toFixed(1)}%)
          </div>
        </div>
        <span className="text-right text-xs tabular-nums text-ink-2">
          {b.value} · {pct(b.value, total)}
        </span>
      </div>
    );
  });
}

function Chart({
  title,
  meta,
  empty,
  children,
}: {
  title: string;
  meta: string;
  empty: boolean;
  children: ReactNode;
}) {
  return (
    <section className="flex min-w-0 flex-col gap-3 rounded-[14px] border border-line bg-surface px-6 pb-5 pt-4 phone:px-4">
      <div className="flex items-baseline justify-between gap-2">
        <h2 className="text-[15px] font-semibold">{title}</h2>
        <span className="text-xs text-ink-3">{meta}</span>
      </div>
      {empty ? (
        <span className="py-6 text-center text-[13px] text-ink-3">No devices on this platform</span>
      ) : (
        children
      )}
    </section>
  );
}

function GroupTitle({ children }: { children: ReactNode }) {
  return (
    <span className="pt-1 text-[11px] font-semibold uppercase tracking-[0.06em] text-ink-3">
      {children}
    </span>
  );
}

export function DeviceStats({ items }: { items: DeviceRow[] }) {
  const [scope, setScope] = useState<Scope>("all");
  const list = items.filter((d) =>
    scope === "all"
      ? true
      : scope === "apple"
        ? familyLabel(d.platform) === "Apple"
        : platformName(d.platform) === scope,
  );
  const n = list.length;
  const count = (fn: (d: DeviceRow) => boolean) => list.filter(fn).length;
  const family = (fam: string) => count((d) => familyLabel(d.platform) === fam);
  const colored = (pairs: [string, number][], color: (label: string) => string): Bar[] =>
    pairs.map(([label, value]) => ({ label, value, color: color(label) }));

  const push = count((d) => d.server_push_enabled);
  const sync = count((d) => !!d.cloud_sync_enabled);
  const now = Date.now();
  const week = count((d) => !!d.last_seen_at && now - new Date(d.last_seen_at).getTime() <= WEEK_MS);
  const kpis = [
    ["Devices", String(n), `${family("Apple")} Apple · ${family("Android")} Android`],
    ["Server push on", pct(push, n), `${push} devices`],
    ["Cloud sync on", pct(sync, n), `${sync} devices`],
    ["Active in 7 days", pct(week, n), `${week} devices`],
  ];

  const platforms: Bar[] = countBy(list, (d) => d.platform).map(([p, value]) => ({
    label: platformName(p),
    value,
    color: familyColor(familyLabel(p)),
  }));
  const apps = ["Apple", "Android"]
    .map((fam) => ({
      fam,
      bars: colored(
        countBy(
          list.filter((d) => familyLabel(d.platform) === fam),
          (d) => d.app_version ?? "—",
        ),
        () => familyColor(fam),
      ),
    }))
    .filter((g) => g.bars.length > 0);
  const os = countBy(list, osName);
  const osBars = colored(os.slice(0, 6), (label) =>
    familyColor(label.startsWith("Android") ? "Android" : "Apple"),
  );
  const osOther = os.slice(6).reduce((sum, [, v]) => sum + v, 0);
  if (osOther) osBars.push({ label: "Other", value: osOther, color: CHART_OTHER });
  const toggles: Bar[] = (
    [
      ["Server push", push],
      ["Cloud sync", sync],
      ["APNs token", count((d) => tokens(d).includes("apns"))],
      ["Push-to-start", count((d) => tokens(d).includes("pts"))],
      ["FCM token", count((d) => tokens(d).includes("fcm"))],
    ] as const
  ).map(([label, value]) => ({ label, value, color: CHART[0] }));

  return (
    <div className="flex flex-col gap-5">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <Segmented label="Platforms" options={SCOPES} value={scope} onChange={setScope} />
        <div className="flex gap-3.5 text-xs text-ink-2">
          {["Apple", "Android"].map((fam) => (
            <span key={fam} className="flex items-center gap-1.5">
              <span
                aria-hidden="true"
                className="size-3 rounded-[3px]"
                style={{ background: familyColor(fam) }}
              />
              {fam}
            </span>
          ))}
        </div>
      </div>

      <div className="grid grid-cols-[repeat(auto-fit,minmax(200px,1fr))] gap-3 phone:grid-cols-2">
        {kpis.map(([label, value, note]) => (
          <div
            key={label}
            className="flex flex-col gap-1 rounded-[14px] border border-line bg-surface px-[18px] py-4"
          >
            <span className="text-xs font-medium text-ink-3">{label}</span>
            <span className="text-[26px] font-semibold tabular-nums tracking-[-0.01em]">{value}</span>
            <span className="text-xs tabular-nums text-ink-2">{note}</span>
          </div>
        ))}
      </div>

      <div className="grid grid-cols-[repeat(auto-fit,minmax(440px,1fr))] gap-5 tablet:grid-cols-1">
        <Chart title="Platform" meta={`${n} devices`} empty={n === 0}>
          <div className="flex flex-col gap-1">
            <Bars bars={platforms} total={n} />
          </div>
        </Chart>
        <Chart title="App version" meta="by build family" empty={n === 0}>
          {apps.map((g) => (
            <div key={g.fam} className="flex flex-col gap-1">
              <GroupTitle>{g.fam}</GroupTitle>
              <Bars bars={g.bars} total={n} />
            </div>
          ))}
        </Chart>
        <Chart title="OS version" meta="top 6" empty={n === 0}>
          <div className="flex flex-col gap-1">
            <Bars bars={osBars} total={n} />
          </div>
        </Chart>
        <Chart title="Push and sync" meta="devices with each on" empty={n === 0}>
          <div className="flex flex-col gap-1">
            <Bars bars={toggles} total={n} />
          </div>
        </Chart>
      </div>
    </div>
  );
}
