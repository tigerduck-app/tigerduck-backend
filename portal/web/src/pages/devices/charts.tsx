// Device-population charts and the stat blocks around them.

import { useState } from "react";
import { Card, CardContent } from "@/components/ui/card";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import type { DeviceRow } from "@/types/api";
import { platformLabel } from "./format";

export const CHART_COLORS = [
  "#3b82f6", "#ef4444", "#22c55e", "#f59e0b", "#8b5cf6",
  "#ec4899", "#14b8a6", "#f97316", "#6366f1", "#84cc16",
];

export function MiniPie({ data }: { data: { label: string; value: number; color: string }[] }) {
  const total = data.reduce((s, d) => s + d.value, 0);
  if (total === 0) return null;
  const size = 140;
  const cx = size / 2;
  const cy = size / 2;
  const r = size / 2 - 4;
  if (data.length === 1) {
    return (
      <svg width={size} height={size} viewBox={`0 0 ${size} ${size}`}>
        <circle cx={cx} cy={cy} r={r} fill={data[0].color} />
      </svg>
    );
  }
  let cumAngle = -90;
  return (
    <svg width={size} height={size} viewBox={`0 0 ${size} ${size}`}>
      {data.map((d, i) => {
        const angle = (d.value / total) * 360;
        const startRad = (cumAngle * Math.PI) / 180;
        const endRad = ((cumAngle + angle) * Math.PI) / 180;
        cumAngle += angle;
        const largeArc = angle > 180 ? 1 : 0;
        const x1 = cx + r * Math.cos(startRad);
        const y1 = cy + r * Math.sin(startRad);
        const x2 = cx + r * Math.cos(endRad);
        const y2 = cy + r * Math.sin(endRad);
        return (
          <path key={i} d={`M ${cx} ${cy} L ${x1} ${y1} A ${r} ${r} 0 ${largeArc} 1 ${x2} ${y2} Z`} fill={d.color} />
        );
      })}
    </svg>
  );
}

export function StatBlock({ title, counts, total }: { title: string; counts: [string, number][]; total: number }) {
  const chartData = counts.map(([label, value], i) => ({
    label, value, color: CHART_COLORS[i % CHART_COLORS.length],
  }));
  return (
    <div className="space-y-3">
      <h4 className="text-sm font-medium">{title}</h4>
      <div className="flex items-start gap-6">
        <MiniPie data={chartData} />
        <div className="space-y-1 text-xs min-w-0">
          {chartData.map((d) => (
            <div key={d.label} className="flex items-center gap-2">
              <span className="inline-block h-2.5 w-2.5 rounded-sm shrink-0" style={{ backgroundColor: d.color }} />
              <span className="truncate">{d.label}</span>
              <span className="text-muted-foreground ml-auto tabular-nums">
                {d.value} ({total > 0 ? ((d.value / total) * 100).toFixed(0) : 0}%)
              </span>
            </div>
          ))}
        </div>
      </div>
    </div>
  );
}

export function DeviceStats({ items }: { items: DeviceRow[] }) {
  const [platformFilter, setPlatformFilter] = useState("all");

  const platforms = ["ios", "ipados", "macos", "android"];
  const APPLE_PLATFORMS = new Set(["ios", "ipados", "macos", "watchos"]);
  const filtered = items.filter((d) => {
    if (platformFilter === "all") return true;
    if (platformFilter === "apple") return APPLE_PLATFORMS.has(d.platform);
    return d.platform === platformFilter;
  });

  const countBy = (key: "os_version" | "app_version") => {
    const map: Record<string, number> = {};
    for (const d of filtered) {
      const v = (key === "os_version" ? `${platformLabel(d.platform)} ${d[key] ?? "?"}` : d[key]) ?? "Unknown";
      map[v] = (map[v] ?? 0) + 1;
    }
    return Object.entries(map).sort((a, b) => b[1] - a[1]);
  };

  return (
    <Card>
      <CardContent className="space-y-6 pt-4">
        <Select value={platformFilter} onValueChange={setPlatformFilter}>
          <SelectTrigger className="w-44">
            <SelectValue />
          </SelectTrigger>
          <SelectContent>
            <SelectItem value="all">All Platforms</SelectItem>
            <SelectItem value="apple">All Apple</SelectItem>
            {platforms.map((p) => (
              <SelectItem key={p} value={p}>{platformLabel(p)}</SelectItem>
            ))}
          </SelectContent>
        </Select>
        {filtered.length === 0 ? (
          <p className="text-sm text-muted-foreground">No devices for this platform filter.</p>
        ) : (
          <div className="grid grid-cols-1 md:grid-cols-2 gap-6">
            <StatBlock title="OS Version" counts={countBy("os_version")} total={filtered.length} />
            <StatBlock title="App Version" counts={countBy("app_version")} total={filtered.length} />
          </div>
        )}
      </CardContent>
    </Card>
  );
}
