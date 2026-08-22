// Aggregate sync counters and the small charts that render them.

import { useQuery } from "@tanstack/react-query";
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";
import { CHART_COLORS, fmt, statusBadge } from "./format";
import type { SyncStatsData } from "./types";

export function SyncStats() {
  const stats = useQuery<SyncStatsData>({
    queryKey: ["moodle-stats"],
    queryFn: () => fetch("/api/moodle/stats").then((r) => r.json()),
    refetchInterval: 10_000,
  });

  const s = stats.data?.summary;
  const runs = stats.data?.recent_runs ?? [];

  return (
    <>
      {s && (
        <Card className="mt-4">
          <CardHeader>
            <CardTitle className="text-base">Last 24h sync results</CardTitle>
            <CardDescription>
              Tick every 30s, batch size 5, per-user interval 8h
            </CardDescription>
          </CardHeader>
          <CardContent>
            <div className="grid grid-cols-3 gap-4 sm:grid-cols-6">
              <div className="text-center">
                <div className="text-2xl font-bold text-green-600">{s.succeeded_24h}</div>
                <div className="text-xs text-muted-foreground">Succeeded</div>
              </div>
              <div className="text-center">
                <div className="text-2xl font-bold text-red-500">{s.failed_24h}</div>
                <div className="text-xs text-muted-foreground">Failed</div>
              </div>
              <div className="text-center">
                <div className="text-2xl font-bold text-blue-500">{s.running_now}</div>
                <div className="text-xs text-muted-foreground">Running</div>
              </div>
              <div className="text-center">
                <div className="text-2xl font-bold">{s.total_24h}</div>
                <div className="text-xs text-muted-foreground">Total runs</div>
              </div>
              <div className="text-center">
                <div className="text-2xl font-bold">{s.avg_duration_s}s</div>
                <div className="text-xs text-muted-foreground">Avg duration</div>
              </div>
              <div className="text-center">
                <div className="text-2xl font-bold">{s.total_fetched}</div>
                <div className="text-xs text-muted-foreground">Items fetched</div>
              </div>
            </div>
          </CardContent>
        </Card>
      )}

      <Card className="mt-4">
        <CardHeader>
          <CardTitle className="text-base">Recent runs (last 50)</CardTitle>
        </CardHeader>
        <CardContent>
          <div className="overflow-x-auto"><Table>
            <TableHeader>
              <TableRow>
                <TableHead>Student</TableHead>
                <TableHead>Type</TableHead>
                <TableHead>Status</TableHead>
                <TableHead>Started</TableHead>
                <TableHead>Duration</TableHead>
                <TableHead className="text-right">Fetched</TableHead>
                <TableHead className="text-right">Changed</TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {runs.length === 0 ? (
                <TableRow>
                  <TableCell colSpan={7} className="text-center text-muted-foreground">
                    No runs in the last 24h
                  </TableCell>
                </TableRow>
              ) : (
                runs.map((r) => {
                  const dur = r.started_at && r.finished_at
                    ? ((new Date(r.finished_at).getTime() - new Date(r.started_at).getTime()) / 1000).toFixed(1) + "s"
                    : "—";
                  return (
                    <TableRow key={r.id}>
                      <TableCell className="font-mono text-xs">{r.student_id}</TableCell>
                      <TableCell className="text-xs">{r.job_type.replace("_", " ")}</TableCell>
                      <TableCell>{statusBadge(r.status)}</TableCell>
                      <TableCell className="text-xs text-muted-foreground">{fmt(r.started_at)}</TableCell>
                      <TableCell className="text-xs font-mono">{dur}</TableCell>
                      <TableCell className="text-right font-mono text-xs">{r.fetched_count ?? "—"}</TableCell>
                      <TableCell className="text-right font-mono text-xs">{r.changed_count ?? "—"}</TableCell>
                    </TableRow>
                  );
                })
              )}
            </TableBody>
          </Table></div>
        </CardContent>
      </Card>
    </>
  );
}

export function MiniPieChart({ data }: { data: { label: string; value: number; color: string }[] }) {
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
          <path
            key={i}
            d={`M ${cx} ${cy} L ${x1} ${y1} A ${r} ${r} 0 ${largeArc} 1 ${x2} ${y2} Z`}
            fill={d.color}
          />
        );
      })}
    </svg>
  );
}

export function StatSection({ title, counts, total }: { title: string; counts: [string, number][]; total: number }) {
  const chartData = counts.map(([label, value], i) => ({
    label,
    value,
    color: CHART_COLORS[i % CHART_COLORS.length],
  }));
  return (
    <div className="space-y-3">
      <h4 className="text-sm font-medium">{title}</h4>
      <div className="flex items-start gap-6">
        <MiniPieChart data={chartData} />
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
