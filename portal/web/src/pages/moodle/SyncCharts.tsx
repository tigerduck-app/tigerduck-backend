// The Sync page's pictures of the queue and of runs over the last day.

import { useState } from "react";
import { Pill } from "@/components/page";
import { cn } from "@/lib/cn";
import { Segmented } from "./controls";
import type { MoodleStatus, SyncStatsData } from "./types";

// The chart palette's first four, in the order the design draws the queue.
export const QUEUE = [
  { key: "active", label: "Active", color: "#2a78d6" },
  { key: "failed", label: "Failed", color: "#eb6834" },
  { key: "running", label: "Running", color: "#1baf7a" },
  { key: "disabled", label: "Disabled", color: "#eda100" },
] as const;
export type QueueKey = (typeof QUEUE)[number]["key"];

const OK = "#2a78d6";
const FAIL = "#eb6834";

/** Segments sized by count; `taken` greys out the ones left out. */
export function QueueBar({
  counts,
  taken,
  className,
  onHover,
}: {
  counts: MoodleStatus["job_counts"];
  taken?: readonly QueueKey[];
  className?: string;
  onHover?: (i: number | null) => void;
}) {
  const total = QUEUE.reduce((n, q) => n + counts[q.key], 0) || 1;
  return (
    <div className={cn("flex gap-0.5", className)}>
      {QUEUE.map((q, i) => (
        <div
          key={q.key}
          onMouseEnter={() => onHover?.(i)}
          onMouseLeave={() => onHover?.(null)}
          className="min-w-[5px] first:rounded-l last:rounded-r"
          style={{
            flex: `${Math.max((counts[q.key] / total) * 100, 1.2)} 1 0`,
            background: !taken || taken.includes(q.key) ? q.color : "var(--color-soft)",
          }}
        />
      ))}
    </div>
  );
}

export function QueueCard({ status }: { status: MoodleStatus }) {
  const [hover, setHover] = useState<number | null>(null);
  const c = status.job_counts;
  const total = QUEUE.reduce((n, q) => n + c[q.key], 0);
  const suspended = !!status.suspended_until;
  const hovered = hover === null ? null : QUEUE[hover];
  return (
    <section aria-labelledby="queue-h" className="flex flex-col gap-3.5 rounded-[14px] border border-line bg-surface px-6 py-[18px] phone:px-4">
      <div className="flex flex-wrap items-baseline justify-between gap-2">
        <div className="flex items-center gap-3">
          <h2 id="queue-h" className="text-[15px] font-semibold">
            Job queue
          </h2>
          <Pill tone={suspended ? "warning" : "success"} dot={suspended ? "warn" : "ok"}>
            {suspended ? "Suspended" : "Active"}
          </Pill>
        </div>
        <span className="text-xs tabular-nums text-ink-3">{total.toLocaleString("en-US")} jobs</span>
      </div>
      <div className="relative">
        <div
          role="img"
          aria-label={`Job queue: ${QUEUE.map((q) => `${c[q.key]} ${q.label.toLowerCase()}`).join(", ")}`}
        >
          <QueueBar counts={c} className="h-5" onHover={setHover} />
        </div>
        {hovered && (
          <div
            className={cn(
              "pointer-events-none absolute top-[26px] z-[5] whitespace-nowrap rounded-lg bg-[#15171A] px-2.5 py-1.5 text-xs text-white",
              hover ? "right-0" : "left-0",
            )}
          >
            {hovered.label}: {c[hovered.key]} ({((c[hovered.key] / (total || 1)) * 100).toFixed(1)}%)
          </div>
        )}
      </div>
      <div className="flex flex-wrap gap-x-5 gap-y-2 text-[13px] text-ink-2">
        {QUEUE.map((q) => (
          <span key={q.key} className="inline-flex items-center gap-1.5">
            <span aria-hidden="true" className="size-2.5 rounded-[3px]" style={{ background: q.color }} />
            {q.label}
            <span className="font-semibold tabular-nums text-ink">{c[q.key].toLocaleString("en-US")}</span>
          </span>
        ))}
      </div>
    </section>
  );
}

export function Kpis({ summary }: { summary: SyncStatsData["summary"] }) {
  const runs = summary.succeeded_24h + summary.failed_24h;
  const kpis: [string, string, string][] = [
    ["Succeeded", summary.succeeded_24h.toLocaleString("en-US"), "last 24 h"],
    [
      "Failed",
      summary.failed_24h.toLocaleString("en-US"),
      `${runs ? ((summary.failed_24h / runs) * 100).toFixed(1) : "0.0"}% of runs`,
    ],
    ["Running", String(summary.running_now), "now"],
    ["Total runs", summary.total_24h.toLocaleString("en-US"), "last 24 h"],
    ["Avg duration", `${summary.avg_duration_s} s`, "finished runs"],
    ["Items fetched", summary.total_fetched.toLocaleString("en-US"), "succeeded runs"],
  ];
  return (
    <div className="grid grid-cols-[repeat(auto-fit,minmax(160px,1fr))] gap-3 tablet:grid-cols-3 phone:grid-cols-2">
      {kpis.map(([label, value, note]) => (
        <div key={label} className="flex flex-col gap-1 rounded-[14px] border border-line bg-surface px-4 py-3.5">
          <span className="text-xs font-medium text-ink-3">{label}</span>
          <span className="text-[22px] font-semibold tracking-[-0.01em] tabular-nums">{value}</span>
          <span className="text-xs text-ink-3">{note}</span>
        </div>
      ))}
    </div>
  );
}

const pad = (n: number) => String(n).padStart(2, "0");
const range = (h: number) => `${pad(h)}:00–${pad((h + 1) % 24)}:00`;

export function RunsChart({ hourly }: { hourly: SyncStatsData["hourly"] }) {
  const [view, setView] = useState<"chart" | "table">("chart");
  const [hover, setHover] = useState<number | null>(null);
  const hours = hourly.map((h) => ({ ...h, at: new Date(h.hour).getHours() }));
  const top = Math.max(3, Math.ceil(Math.max(0, ...hours.map((h) => h.succeeded + h.failed)) / 3) * 3);
  const shown = hover === null ? null : hours[hover];
  const legend = (color: string, label: string) => (
    <span className="inline-flex items-center gap-1.5 text-xs text-ink-2">
      <span aria-hidden="true" className="size-2.5 rounded-[3px]" style={{ background: color }} />
      {label}
    </span>
  );

  return (
    <section aria-labelledby="runs-chart-h" className="overflow-hidden rounded-[14px] border border-line bg-surface phone:hidden">
      <div className="flex flex-wrap items-center justify-between gap-3 border-b border-line px-6 py-4">
        <div className="flex flex-wrap items-center gap-4">
          <h2 id="runs-chart-h" className="text-[15px] font-semibold">
            Runs per hour, last 24 h
          </h2>
          {legend(OK, "Succeeded")}
          {legend(FAIL, "Failed")}
        </div>
        <Segmented
          label="View"
          value={view}
          onChange={(v) => {
            setView(v);
            setHover(null);
          }}
          options={[
            { value: "chart", label: "Chart" },
            { value: "table", label: "Table" },
          ]}
        />
      </div>
      {view === "chart" ? (
        <div className="flex gap-2.5 px-6 pb-4 pt-5">
          <div aria-hidden="true" className="flex h-[200px] flex-col justify-between text-right text-[11px] tabular-nums leading-[0] text-ink-3">
            {[top, (top * 2) / 3, top / 3, 0].map((v) => (
              <span key={v}>{v}</span>
            ))}
          </div>
          <div className="flex min-w-0 flex-1 flex-col gap-2">
            <div className="relative h-[200px]">
              {["top-0", "top-1/3", "top-2/3"].map((t) => (
                <div key={t} aria-hidden="true" className={cn("absolute inset-x-0 border-t border-divider", t)} />
              ))}
              <div aria-hidden="true" className="absolute inset-x-0 bottom-0 border-t border-field" />
              <div
                role="img"
                aria-label="Runs per hour; switch to Table for the numbers"
                className="absolute inset-0 flex items-stretch gap-1"
              >
                {hours.map((h, i) => (
                  <div
                    key={h.hour}
                    onMouseEnter={() => setHover(i)}
                    onMouseLeave={() => setHover(null)}
                    className={cn(
                      "flex min-w-0 flex-1 flex-col-reverse gap-0.5 rounded",
                      hover === i && "bg-chart-1/10",
                    )}
                  >
                    <div
                      className={h.failed ? "" : "rounded-t"}
                      style={{ height: `${(h.succeeded / top) * 100}%`, background: OK }}
                    />
                    {h.failed > 0 && (
                      <div className="rounded-t" style={{ height: `${(h.failed / top) * 100}%`, background: FAIL }} />
                    )}
                  </div>
                ))}
              </div>
              {shown && hover !== null && (
                <div
                  className="pointer-events-none absolute top-2 z-[5] min-w-[150px] rounded-[10px] bg-[#15171A] px-2.5 py-2 text-xs text-white shadow-[0_8px_24px_rgba(20,22,26,0.24)]"
                  style={hover > 15 ? { right: `${((23 - hover) / 24) * 100 + 2}%` } : { left: `${(hover / 24) * 100 + 5}%` }}
                >
                  <div className="mb-1 font-semibold">{range(shown.at)}</div>
                  {(
                    [
                      [OK, "Succeeded", shown.succeeded],
                      [FAIL, "Failed", shown.failed],
                    ] as const
                  ).map(([color, label, n]) => (
                    <div key={label} className="flex items-center gap-1.5">
                      <span aria-hidden="true" className="size-2 rounded-sm" style={{ background: color }} />
                      {label}
                      <span className="ml-auto pl-3 tabular-nums">{n}</span>
                    </div>
                  ))}
                </div>
              )}
            </div>
            <div aria-hidden="true" className="flex gap-1 text-[11px] tabular-nums text-ink-3">
              {hours.map((h, i) => (
                <span key={h.hour} className="min-w-0 flex-1 text-center">
                  {i % 3 === 0 ? pad(h.at) : ""}
                </span>
              ))}
            </div>
          </div>
        </div>
      ) : (
        <div className="max-h-[260px] overflow-y-auto">
          <table className="w-full border-collapse text-[13px]">
            <thead>
              <tr className="sticky top-0 bg-surface text-left text-xs text-ink-3">
                <th scope="col" className="px-6 py-2 font-medium">Hour</th>
                <th scope="col" className="px-3 py-2 text-right font-medium">Succeeded</th>
                <th scope="col" className="py-2 pl-3 pr-6 text-right font-medium">Failed</th>
              </tr>
            </thead>
            <tbody>
              {hours.map((h) => (
                <tr key={h.hour} className="border-t border-divider tabular-nums">
                  <td className="px-6 py-2">{range(h.at)}</td>
                  <td className="px-3 py-2 text-right">{h.succeeded}</td>
                  <td className="py-2 pl-3 pr-6 text-right">{h.failed}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </section>
  );
}
