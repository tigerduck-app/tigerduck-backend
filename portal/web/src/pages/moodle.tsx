import { useEffect, useRef, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useHashTab } from "@/hooks/use-hash-tab";
import { toast } from "sonner";
import {
  AlertCircle,
  CheckCircle2,
  Loader2,
  Pause,
  Play,
  RefreshCw,
  Search,
  Users,
} from "lucide-react";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card";
import { Checkbox } from "@/components/ui/checkbox";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs";
import { Input } from "@/components/ui/input";
import { PageHeader, Section } from "@/components/ui/section";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";

type MoodleStatus = {
  suspended_until: string | null;
  job_counts: {
    active: number;
    running: number;
    failed: number;
    disabled: number;
  };
};

type Student = {
  student_id: string;
  credential_status: string;
  last_auth_success_at: string | null;
  last_auth_failure_at: string | null;
  last_auth_error: string | null;
  updated_at: string;
};

type StudentsResponse = {
  counts: { valid: number; expired: number };
  students: Student[];
};

function fmt(iso: string | null): string {
  if (!iso) return "—";
  return new Date(iso).toLocaleString();
}

export function MoodlePage() {
  const [activeTab, setActiveTab] = useHashTab("actions");
  return (
    <>
      <PageHeader
        title="Moodle Sync"
        description="Manage server-side Moodle sync jobs"
      />
      <Tabs value={activeTab} onValueChange={setActiveTab} className="space-y-4">
        <TabsList>
          <TabsTrigger value="actions">Actions</TabsTrigger>
          <TabsTrigger value="lists">Lists</TabsTrigger>
          <TabsTrigger value="sync">Sync</TabsTrigger>
        </TabsList>
        <TabsContent value="actions">
          <ActionsTab />
        </TabsContent>
        <TabsContent value="lists">
          <ListsTab />
        </TabsContent>
        <TabsContent value="sync">
          <SyncTab />
        </TabsContent>
      </Tabs>
    </>
  );
}

function ActionsTab() {
  const qc = useQueryClient();
  const [hours, setHours] = useState("4");
  const [notifyOnRetry, setNotifyOnRetry] = useState(false);

  const status = useQuery<MoodleStatus>({
    queryKey: ["moodle-status"],
    queryFn: () => fetch("/api/moodle/status").then((r) => r.json()),
    refetchInterval: 10_000,
  });

  const suspendMut = useMutation({
    mutationFn: () =>
      fetch("/api/moodle/suspend", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ hours: parseFloat(hours) }),
      }).then((r) => r.json()),
    onSuccess: () => {
      toast.success(`Moodle sync suspended for ${hours}h`);
      qc.invalidateQueries({ queryKey: ["moodle-status"] });
    },
  });

  const resumeMut = useMutation({
    mutationFn: () =>
      fetch("/api/moodle/resume", { method: "POST" }).then((r) => r.json()),
    onSuccess: () => {
      toast.success("Moodle sync resumed");
      qc.invalidateQueries({ queryKey: ["moodle-status"] });
    },
  });

  const retryMut = useMutation({
    mutationFn: () =>
      fetch("/api/moodle/retry-all", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ notify: notifyOnRetry }),
      }).then((r) => r.json()),
    onSuccess: (data: { retried: number; notified: number }) => {
      const msg = data.notified
        ? `Retried ${data.retried} job(s), notified ${data.notified} user(s)`
        : `Retried ${data.retried} job(s)`;
      toast.success(msg);
      qc.invalidateQueries({ queryKey: ["moodle-status"] });
      qc.invalidateQueries({ queryKey: ["moodle-students"] });
    },
  });

  const isSuspended = !!status.data?.suspended_until;
  const c = status.data?.job_counts;

  return (
    <Section>
      <Card>
        <CardHeader>
          <CardTitle className="flex items-center gap-2">
            {isSuspended ? (
              <>
                <Pause className="h-5 w-5 text-orange-500" />
                Suspended
              </>
            ) : (
              <>
                <CheckCircle2 className="h-5 w-5 text-green-500" />
                Active
              </>
            )}
          </CardTitle>
          {isSuspended && (
            <CardDescription>
              Suspended until {fmt(status.data!.suspended_until)}
            </CardDescription>
          )}
        </CardHeader>
        <CardContent className="space-y-4">
          {c && (
            <div className="flex gap-2 text-sm">
              <Badge variant="default">{c.active + c.running} active</Badge>
              {c.failed > 0 && (
                <Badge variant="destructive">{c.failed} failed</Badge>
              )}
              {c.disabled > 0 && (
                <Badge variant="outline">{c.disabled} disabled</Badge>
              )}
            </div>
          )}

          <div className="flex flex-wrap items-center gap-3 border-t pt-4">
            <span className="text-sm font-medium">Suspend sync</span>
            {isSuspended ? (
              <Button
                size="sm"
                onClick={() => resumeMut.mutate()}
                disabled={resumeMut.isPending}
              >
                <Play className="mr-1.5 h-3.5 w-3.5" />
                Resume now
              </Button>
            ) : (
              <>
                <Select value={hours} onValueChange={setHours}>
                  <SelectTrigger className="w-24">
                    <SelectValue />
                  </SelectTrigger>
                  <SelectContent>
                    <SelectItem value="1">1 h</SelectItem>
                    <SelectItem value="2">2 h</SelectItem>
                    <SelectItem value="4">4 h</SelectItem>
                    <SelectItem value="8">8 h</SelectItem>
                    <SelectItem value="12">12 h</SelectItem>
                    <SelectItem value="24">24 h</SelectItem>
                  </SelectContent>
                </Select>
                <Button
                  size="sm"
                  variant="outline"
                  onClick={() => suspendMut.mutate()}
                  disabled={suspendMut.isPending}
                >
                  <Pause className="mr-1.5 h-3.5 w-3.5" />
                  Suspend
                </Button>
              </>
            )}
          </div>

          <div className="flex flex-wrap items-center gap-3 border-t pt-4">
            <span className="text-sm font-medium">Retry all failed</span>
            <div className="flex items-center gap-2">
              <Checkbox
                id="notify-retry"
                checked={notifyOnRetry}
                onCheckedChange={(v) => setNotifyOnRetry(v === true)}
              />
              <label
                htmlFor="notify-retry"
                className="text-sm text-muted-foreground"
              >
                Send expire notification to users
              </label>
            </div>
            <Button
              size="sm"
              variant="outline"
              onClick={() => retryMut.mutate()}
              disabled={retryMut.isPending}
            >
              <RefreshCw className="mr-1.5 h-3.5 w-3.5" />
              Retry all
            </Button>
          </div>
        </CardContent>
      </Card>

      <SyncStats />
      <JobsTable />
    </Section>
  );
}

type SyncStatsData = {
  summary: {
    succeeded_24h: number;
    failed_24h: number;
    running_now: number;
    total_24h: number;
    avg_duration_s: number;
    total_fetched: number;
  };
  recent_runs: {
    id: number;
    student_id: string;
    job_type: string;
    status: string;
    started_at: string | null;
    finished_at: string | null;
    fetched_count: number | null;
    changed_count: number | null;
  }[];
};

function SyncStats() {
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
          <Table>
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
          </Table>
        </CardContent>
      </Card>
    </>
  );
}

type GlobalSyncJob = {
  id: number;
  student_id: string;
  job_type: string;
  status: string;
  priority: number;
  run_after: string | null;
  last_success_at: string | null;
  last_error: string | null;
  attempts: number;
};

function statusBadge(s: string) {
  switch (s) {
    case "pending": return <Badge variant="default">pending</Badge>;
    case "running": return <Badge className="bg-blue-600">running</Badge>;
    case "failed": return <Badge variant="destructive">failed</Badge>;
    case "disabled": return <Badge variant="outline">disabled</Badge>;
    default: return <Badge variant="secondary">{s}</Badge>;
  }
}

function JobsTable() {
  const jobs = useQuery<{ jobs: GlobalSyncJob[] }>({
    queryKey: ["moodle-jobs"],
    queryFn: () => fetch("/api/moodle/jobs").then((r) => r.json()),
    refetchInterval: 10_000,
  });

  const list = jobs.data?.jobs ?? [];

  return (
    <Card className="mt-4">
      <CardHeader>
        <CardTitle className="text-base">Sync jobs</CardTitle>
        <CardDescription>{list.length} job(s)</CardDescription>
      </CardHeader>
      <CardContent>
        <Table>
          <TableHeader>
            <TableRow>
              <TableHead>Student</TableHead>
              <TableHead>Type</TableHead>
              <TableHead>Status</TableHead>
              <TableHead>Next run</TableHead>
              <TableHead>Last success</TableHead>
              <TableHead>Error</TableHead>
              <TableHead className="text-right">Attempts</TableHead>
            </TableRow>
          </TableHeader>
          <TableBody>
            {list.length === 0 ? (
              <TableRow>
                <TableCell colSpan={7} className="text-center text-muted-foreground">
                  No sync jobs
                </TableCell>
              </TableRow>
            ) : (
              list.map((j) => (
                <TableRow key={j.id}>
                  <TableCell className="font-mono text-xs">{j.student_id}</TableCell>
                  <TableCell className="text-xs">{j.job_type.replace("_", " ")}</TableCell>
                  <TableCell>{statusBadge(j.status)}</TableCell>
                  <TableCell className="text-xs text-muted-foreground">{fmt(j.run_after)}</TableCell>
                  <TableCell className="text-xs text-muted-foreground">{fmt(j.last_success_at)}</TableCell>
                  <TableCell className="text-xs text-red-500 max-w-[150px] truncate">{j.last_error ?? "—"}</TableCell>
                  <TableCell className="text-right font-mono text-xs">{j.attempts}</TableCell>
                </TableRow>
              ))
            )}
          </TableBody>
        </Table>
      </CardContent>
    </Card>
  );
}

function ListsTab() {
  const [credFilter, setCredFilter] = useState<string>("all");

  const students = useQuery<StudentsResponse>({
    queryKey: ["moodle-students", credFilter],
    queryFn: () => {
      const params = new URLSearchParams();
      if (credFilter !== "all") params.set("credential_filter", credFilter);
      return fetch(`/api/moodle/students?${params}`).then((r) => r.json());
    },
    refetchInterval: 15_000,
  });

  const c = students.data?.counts;

  return (
    <Section>
      <Card>
        <CardHeader>
          <CardTitle className="flex items-center gap-2">
            <Users className="h-5 w-5" />
            Students
          </CardTitle>
          <CardDescription>
            <div className="flex items-center gap-3">
              {c && (
                <div className="flex gap-2 text-sm">
                  <Badge
                    variant="default"
                    className="bg-green-500/10 text-green-600"
                  >
                    {c.valid} valid
                  </Badge>
                  <Badge
                    variant="default"
                    className="bg-red-500/10 text-red-600"
                  >
                    {c.expired} expired
                  </Badge>
                  <Badge variant="outline">
                    {c.valid + c.expired} total
                  </Badge>
                </div>
              )}
              <Select value={credFilter} onValueChange={setCredFilter}>
                <SelectTrigger className="w-32">
                  <SelectValue />
                </SelectTrigger>
                <SelectContent>
                  <SelectItem value="all">All</SelectItem>
                  <SelectItem value="valid">Valid</SelectItem>
                  <SelectItem value="expired">Expired</SelectItem>
                </SelectContent>
              </Select>
            </div>
          </CardDescription>
        </CardHeader>
        <CardContent>
          <Table>
            <TableHeader>
              <TableRow>
                <TableHead>Student ID</TableHead>
                <TableHead>Status</TableHead>
                <TableHead>Last success</TableHead>
                <TableHead>Last failure</TableHead>
                <TableHead>Error</TableHead>
                <TableHead>Updated</TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {students.data?.students.map((s) => (
                <TableRow key={s.student_id}>
                  <TableCell className="font-mono text-xs">
                    {s.student_id}
                  </TableCell>
                  <TableCell>
                    <CredentialBadge status={s.credential_status} />
                  </TableCell>
                  <TableCell className="text-xs">
                    {fmt(s.last_auth_success_at)}
                  </TableCell>
                  <TableCell className="text-xs">
                    {fmt(s.last_auth_failure_at)}
                  </TableCell>
                  <TableCell className="max-w-[180px] truncate text-xs text-destructive">
                    {s.last_auth_error ?? "—"}
                  </TableCell>
                  <TableCell className="text-xs">
                    {fmt(s.updated_at)}
                  </TableCell>
                </TableRow>
              ))}
              {students.data?.students.length === 0 && (
                <TableRow>
                  <TableCell
                    colSpan={6}
                    className="text-center text-muted-foreground"
                  >
                    No students found
                  </TableCell>
                </TableRow>
              )}
            </TableBody>
          </Table>
        </CardContent>
      </Card>
    </Section>
  );
}

type SyncJob = {
  id: number;
  job_type: string;
  job_status: string;
  attempts: number;
  last_success_at: string | null;
  last_failure_at: string | null;
  last_error: string | null;
  run_after: string | null;
};

type SyncRun = {
  id: number;
  job_type: string;
  started_at: string;
  finished_at: string | null;
  status: string;
  fetched_count: number | null;
  changed_count: number | null;
  error: string | null;
  meta: Record<string, unknown> | null;
};

type SyncOverride = {
  moodle_assignment_id: number;
  title: string | null;
  local_status: string;
  updated_at: string;
};

type SyncCourse = {
  id: number;
  moodle_id: string | null;
  course_no: string;
  course_name: string;
  course_name_en: string | null;
  client_course_no: string;
  source: string;
  color_hex: string | null;
  custom_names: Record<string, string> | null;
  default_palette_index: number;
  default_color_light: string;
  default_color_dark: string;
};

type SyncCoursesResponse = {
  semester: string;
  palette_light: string[];
  palette_dark: string[];
  courses: SyncCourse[];
};

type SyncDevice = {
  id: string;
  client_device_id: string;
  platform: string;
  app_version: string | null;
  os_version: string | null;
  last_seen_at: string | null;
  last_login_at: string | null;
  created_at: string | null;
};

type PushJobRow = {
  id: number;
  scenario: string;
  status: string;
  attempts: number;
  max_attempts: number;
  fire_at: string;
  sent_at: string | null;
  last_error: string | null;
  dedupe_key: string;
  created_at: string;
  source_device_id: string | null;
};

type PushDeliveryRow = {
  id: number;
  push_job_id: number;
  device_id: string | null;
  provider: string;
  status: string;
  attempts: number;
  max_attempts: number;
  failure_code: string | null;
  failure_message: string | null;
  sent_at: string | null;
  created_at: string;
};

type SyncEventsResponse = {
  student_id: string;
  found: boolean;
  jobs?: SyncJob[];
  runs?: SyncRun[];
  overrides?: SyncOverride[];
  devices?: SyncDevice[];
  push_jobs?: PushJobRow[];
  push_deliveries?: PushDeliveryRow[];
};

type LogEntry = {
  id: number;
  ts: string;
  level: string;
  source: string;
  message: string;
  detail: Record<string, unknown> | null;
  device_label: string | null;
  platform: string | null;
};

type LogsResponse = {
  entries: LogEntry[];
  latest_id: number;
};

const CHART_COLORS = [
  "#3b82f6", "#ef4444", "#22c55e", "#f59e0b", "#8b5cf6",
  "#ec4899", "#14b8a6", "#f97316", "#6366f1", "#84cc16",
];
const PLATFORM_LABELS: Record<string, string> = {
  ios: "iOS", ipados: "iPadOS", macos: "macOS", watchos: "watchOS",
  android: "Android", wearos: "Wear OS", web: "Web",
};
function platformLabel(p: string) { return PLATFORM_LABELS[p] ?? p; }

function MiniPieChart({ data }: { data: { label: string; value: number; color: string }[] }) {
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

function StatSection({ title, counts, total }: { title: string; counts: [string, number][]; total: number }) {
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

function DevicesCard({ devices, pushJobs, pushDeliveries, studentId }: { devices: SyncDevice[]; pushJobs?: PushJobRow[]; pushDeliveries?: PushDeliveryRow[]; studentId: string }) {
  const [platformFilter, setPlatformFilter] = useState("all");

  const platforms = ["ios", "ipados", "macos", "android"];

  const filtered = devices.filter((d) => {
    if (platformFilter === "all") return true;
    if (platformFilter === "apple") return ["ios", "ipados", "macos", "watchos"].includes(d.platform);
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
      <CardHeader>
        <CardTitle className="text-base">Devices ({devices.length})</CardTitle>
      </CardHeader>
      <CardContent>
        <Tabs defaultValue="list">
          <TabsList>
            <TabsTrigger value="list">List</TabsTrigger>
            <TabsTrigger value="stats">Statistics</TabsTrigger>
            <TabsTrigger value="push">Push Queue{pushJobs && pushJobs.length > 0 ? ` (${pushJobs.length})` : ""}</TabsTrigger>
          </TabsList>
          <TabsContent value="list">
            <Table>
              <TableHeader>
                <TableRow>
                  <TableHead>Platform</TableHead>
                  <TableHead>Device ID</TableHead>
                  <TableHead>App Version</TableHead>
                  <TableHead>OS</TableHead>
                  <TableHead>Last Seen</TableHead>
                  <TableHead>Registered</TableHead>
                </TableRow>
              </TableHeader>
              <TableBody>
                {devices.map((d) => (
                  <TableRow key={d.id}>
                    <TableCell>
                      <Badge variant="outline">{d.platform}</Badge>
                    </TableCell>
                    <TableCell className="font-mono text-xs text-muted-foreground break-all">
                      {d.client_device_id}
                    </TableCell>
                    <TableCell className="text-xs">{d.app_version ?? "—"}</TableCell>
                    <TableCell className="text-xs">{d.os_version ? `${platformLabel(d.platform)} ${d.os_version}` : "—"}</TableCell>
                    <TableCell className="text-xs">{fmt(d.last_seen_at)}</TableCell>
                    <TableCell className="text-xs">{fmt(d.created_at)}</TableCell>
                  </TableRow>
                ))}
              </TableBody>
            </Table>
          </TabsContent>
          <TabsContent value="stats">
            <div className="mb-4">
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
            </div>
            {filtered.length === 0 ? (
              <p className="text-sm text-muted-foreground">No devices for this platform filter.</p>
            ) : (
              <div className="grid grid-cols-1 md:grid-cols-2 gap-6">
                <StatSection title="OS Version" counts={countBy("os_version")} total={filtered.length} />
                <StatSection title="App Version" counts={countBy("app_version")} total={filtered.length} />
              </div>
            )}
          </TabsContent>
          <TabsContent value="push">
            <div className="mb-4 flex flex-wrap gap-2">
              {pushJobs && pushJobs.length > 0 && (
                <>
                  <Button
                    size="sm"
                    variant="outline"
                    onClick={async () => {
                      try {
                        const res = await fetch("/api/moodle/push-tick", { method: "POST" });
                        const data = await res.json();
                        if (!data.ok) alert("Push tick failed: " + (data.error ?? "unknown"));
                      } catch (e) {
                        alert("Push tick request failed");
                      }
                    }}
                  >
                    Force Execute Pipeline
                  </Button>
                  <Button
                    size="sm"
                    variant="outline"
                    onClick={async () => {
                      if (!confirm(`Cancel all ${pushJobs.length} pending push jobs?`)) return;
                      try {
                        const res = await fetch(`/api/moodle/push-clear?student_id=${encodeURIComponent(studentId)}`, { method: "POST" });
                        const data = await res.json();
                        if (!data.ok) alert("Clear failed: " + (data.error ?? "unknown"));
                      } catch (e) {
                        alert("Clear request failed");
                      }
                    }}
                  >
                    Clear Queue
                  </Button>
                </>
              )}
              <Button
                size="sm"
                variant="default"
                onClick={async () => {
                  try {
                    const res = await fetch(`/api/moodle/push-sync-trigger?student_id=${encodeURIComponent(studentId)}`, { method: "POST" });
                    const data = await res.json();
                    if (data.deduplicated) alert("Deduplicated — a sync_trigger already exists in this window");
                    else if (!data.ok) alert("Failed: " + (data.error ?? "unknown"));
                  } catch (e) {
                    alert("Request failed");
                  }
                }}
              >
                Force Sync Push
              </Button>
            </div>
            {!pushJobs || pushJobs.length === 0 ? (
              <p className="text-sm text-muted-foreground py-4">No pending push jobs.</p>
            ) : (
              <div className="space-y-4">
                {pushJobs.map((pj) => {
                  const deliveries = (pushDeliveries ?? []).filter((d) => d.push_job_id === pj.id);
                  const sourceDevice = devices.find((d) => d.id === pj.source_device_id);
                  return (
                    <div key={pj.id} className="border rounded-md p-3 space-y-2">
                      <div className="flex items-center gap-2 flex-wrap">
                        <Badge variant={pj.status === "pending" ? "default" : "secondary"}>{pj.status}</Badge>
                        <span className="font-mono text-xs">{pj.scenario}</span>
                        <span className="text-xs text-muted-foreground ml-auto">
                          #{pj.id} &middot; {pj.attempts}/{pj.max_attempts} attempts &middot; created {fmt(pj.created_at)}
                        </span>
                      </div>
                      {sourceDevice && (
                        <p className="text-xs text-muted-foreground">
                          Source: <Badge variant="outline" className="text-[10px] px-1 py-0">{sourceDevice.platform}</Badge> {sourceDevice.client_device_id.slice(0, 8)}...
                        </p>
                      )}
                      {pj.last_error && <p className="text-xs text-destructive">{pj.last_error}</p>}
                      {deliveries.length > 0 && (
                        <Table>
                          <TableHeader>
                            <TableRow>
                              <TableHead className="text-xs">Target Device</TableHead>
                              <TableHead className="text-xs">Provider</TableHead>
                              <TableHead className="text-xs">Status</TableHead>
                              <TableHead className="text-xs">Attempts</TableHead>
                              <TableHead className="text-xs">Error</TableHead>
                            </TableRow>
                          </TableHeader>
                          <TableBody>
                            {deliveries.map((dl) => {
                              const targetDevice = devices.find((d) => d.id === dl.device_id);
                              return (
                                <TableRow key={dl.id}>
                                  <TableCell className="text-xs">
                                    {targetDevice ? (
                                      <><Badge variant="outline" className="text-[10px] px-1 py-0">{targetDevice.platform}</Badge> {targetDevice.client_device_id.slice(0, 8)}...</>
                                    ) : (
                                      <span className="text-muted-foreground">{dl.device_id?.slice(0, 8) ?? "—"}...</span>
                                    )}
                                  </TableCell>
                                  <TableCell><Badge variant="outline" className="text-[10px]">{dl.provider}</Badge></TableCell>
                                  <TableCell>
                                    <Badge variant={dl.status === "sent" ? "default" : dl.status === "failed" ? "destructive" : "secondary"} className="text-[10px]">
                                      {dl.status}
                                    </Badge>
                                  </TableCell>
                                  <TableCell className="text-xs">{dl.attempts}/{dl.max_attempts}</TableCell>
                                  <TableCell className="text-xs text-destructive">{dl.failure_code ?? "—"}</TableCell>
                                </TableRow>
                              );
                            })}
                          </TableBody>
                        </Table>
                      )}
                      {deliveries.length === 0 && (
                        <p className="text-xs text-muted-foreground">Not yet materialized (waiting for pipeline tick)</p>
                      )}
                    </div>
                  );
                })}
              </div>
            )}
          </TabsContent>
        </Tabs>
      </CardContent>
    </Card>
  );
}

function SyncTab() {
  const [studentId, setStudentId] = useState("");
  const [query, setQuery] = useState("");
  const [logEntries, setLogEntries] = useState<LogEntry[]>([]);
  const [latestId, setLatestId] = useState(0);
  const [courseNameLang, setCourseNameLang] = useState<"en" | "zh">("en");
  const logEndRef = useRef<HTMLDivElement>(null);

  const events = useQuery<SyncEventsResponse>({
    queryKey: ["sync-events", query],
    queryFn: () =>
      fetch(`/api/moodle/sync-events?student_id=${encodeURIComponent(query)}`).then((r) => r.json()),
    enabled: query.length > 0,
    refetchInterval: query ? 5_000 : false,
  });

  const logsQuery = useQuery<LogsResponse>({
    queryKey: ["sync-logs", query, latestId],
    queryFn: () =>
      fetch(`/api/moodle/sync-logs?student_id=${encodeURIComponent(query)}&after_id=${latestId}`).then((r) => r.json()),
    enabled: query.length > 0,
    refetchInterval: query ? 2_000 : false,
  });

  useEffect(() => {
    const d = logsQuery.data;
    if (d && d.entries.length > 0) {
      setLogEntries((prev) => [...prev, ...d.entries].slice(-500));
      setLatestId(d.latest_id);
      setTimeout(() => logEndRef.current?.scrollIntoView({ behavior: "smooth" }), 50);
    }
  }, [logsQuery.data]);

  const handleSearch = () => {
    const trimmed = studentId.trim();
    if (trimmed) {
      setQuery(trimmed);
      setLogEntries([]);
      setLatestId(0);
    }
  };

  const coursesQuery = useQuery<SyncCoursesResponse>({
    queryKey: ["sync-courses", query],
    queryFn: () =>
      fetch(`/api/moodle/sync-courses?student_id=${encodeURIComponent(query)}`).then((r) => r.json()),
    enabled: query.length > 0,
    refetchInterval: query ? 10_000 : false,
  });

  const data = events.data;
  const coursesData = coursesQuery.data;

  return (
    <Section className="space-y-4">
      <Card>
        <CardHeader>
          <CardTitle className="flex items-center gap-2">
            <Search className="h-5 w-5" />
            Sync Events
          </CardTitle>
          <CardDescription>
            Enter a student ID to view sync job history, run logs, and override state.
          </CardDescription>
        </CardHeader>
        <CardContent>
          <form
            className="flex items-center gap-2"
            onSubmit={(e) => { e.preventDefault(); handleSearch(); }}
          >
            <Input
              placeholder="Student ID (e.g. B11234567)"
              value={studentId}
              onChange={(e) => setStudentId(e.target.value)}
              className="max-w-xs font-mono"
            />
            <Button type="submit" size="sm" disabled={!studentId.trim() || events.isFetching}>
              {events.isFetching ? <Loader2 className="mr-1.5 h-3.5 w-3.5 animate-spin" /> : <Play className="mr-1.5 h-3.5 w-3.5" />}
              Start monitoring
            </Button>
          </form>
        </CardContent>
      </Card>

      {data && !data.found && (
        <Card>
          <CardContent className="py-6 text-center text-muted-foreground">
            Student "{data.student_id}" not found
          </CardContent>
        </Card>
      )}

      {query && data?.found !== false && (
        <Card>
          <CardHeader>
            <CardTitle className="text-base">Sync Jobs</CardTitle>
          </CardHeader>
          <CardContent>
            {!data ? (
              <div className="flex items-center justify-center py-6 text-muted-foreground">
                <Loader2 className="mr-2 h-4 w-4 animate-spin" /> Loading…
              </div>
            ) : (
            <Table>
              <TableHeader>
                <TableRow>
                  <TableHead>Job Type</TableHead>
                  <TableHead>Status</TableHead>
                  <TableHead>Attempts</TableHead>
                  <TableHead>Last Success</TableHead>
                  <TableHead>Last Failure</TableHead>
                  <TableHead>Error</TableHead>
                  <TableHead>Next Run</TableHead>
                </TableRow>
              </TableHeader>
              <TableBody>
                {(data.jobs ?? []).map((j) => (
                  <TableRow key={j.id}>
                    <TableCell className="font-mono text-xs">{j.job_type}</TableCell>
                    <TableCell><RunStatusBadge status={j.job_status} /></TableCell>
                    <TableCell>{j.attempts}</TableCell>
                    <TableCell className="text-xs">{fmt(j.last_success_at)}</TableCell>
                    <TableCell className="text-xs">{fmt(j.last_failure_at)}</TableCell>
                    <TableCell className="max-w-[200px] truncate text-xs text-destructive">{j.last_error ?? "—"}</TableCell>
                    <TableCell className="text-xs">{fmt(j.run_after)}</TableCell>
                  </TableRow>
                ))}
                {(data.jobs ?? []).length === 0 && (
                  <TableRow><TableCell colSpan={7} className="text-center text-muted-foreground">No sync jobs</TableCell></TableRow>
                )}
              </TableBody>
            </Table>
            )}
          </CardContent>
        </Card>
      )}

      {data?.found && data.devices && data.devices.length > 0 && (
        <DevicesCard devices={data.devices} pushJobs={data.push_jobs} pushDeliveries={data.push_deliveries} studentId={query} />
      )}

      {query && data?.found !== false && (
        <Card>
          <CardHeader>
            <CardTitle className="text-base">Run History{data?.runs ? ` (${data.runs.length})` : ""}</CardTitle>
          </CardHeader>
          <CardContent>
            {!data ? (
              <div className="flex items-center justify-center py-6 text-muted-foreground">
                <Loader2 className="mr-2 h-4 w-4 animate-spin" /> Loading…
              </div>
            ) : (
            <Table>
              <TableHeader>
                <TableRow>
                  <TableHead>Started</TableHead>
                  <TableHead>Job Type</TableHead>
                  <TableHead>Status</TableHead>
                  <TableHead>Duration</TableHead>
                  <TableHead>Fetched</TableHead>
                  <TableHead>Changed</TableHead>
                  <TableHead>Error</TableHead>
                </TableRow>
              </TableHeader>
              <TableBody>
                {(data.runs ?? []).map((r) => {
                  const dur = r.finished_at && r.started_at
                    ? `${((new Date(r.finished_at).getTime() - new Date(r.started_at).getTime()) / 1000).toFixed(1)}s`
                    : "—";
                  return (
                    <TableRow key={r.id}>
                      <TableCell className="text-xs">{fmt(r.started_at)}</TableCell>
                      <TableCell className="font-mono text-xs">{r.job_type}</TableCell>
                      <TableCell><RunStatusBadge status={r.status} /></TableCell>
                      <TableCell className="text-xs">{dur}</TableCell>
                      <TableCell>{r.fetched_count ?? "—"}</TableCell>
                      <TableCell>{r.changed_count ?? "—"}</TableCell>
                      <TableCell className="max-w-[250px] truncate text-xs text-destructive">{r.error ?? "—"}</TableCell>
                    </TableRow>
                  );
                })}
                {(data.runs ?? []).length === 0 && (
                  <TableRow><TableCell colSpan={7} className="text-center text-muted-foreground">No sync runs yet</TableCell></TableRow>
                )}
              </TableBody>
            </Table>
            )}
          </CardContent>
        </Card>
      )}

      {data?.found && data.overrides && data.overrides.length > 0 && (
        <Card>
          <CardHeader>
            <CardTitle className="text-base">Assignment Overrides ({data.overrides.length})</CardTitle>
          </CardHeader>
          <CardContent>
            <Table>
              <TableHeader>
                <TableRow>
                  <TableHead>Moodle ID</TableHead>
                  <TableHead>Title</TableHead>
                  <TableHead>Status</TableHead>
                  <TableHead>Updated</TableHead>
                </TableRow>
              </TableHeader>
              <TableBody>
                {data.overrides.map((o) => (
                  <TableRow key={o.moodle_assignment_id}>
                    <TableCell className="font-mono text-xs">{o.moodle_assignment_id}</TableCell>
                    <TableCell className="text-xs max-w-[200px] truncate">{o.title ?? "—"}</TableCell>
                    <TableCell><RunStatusBadge status={o.local_status} /></TableCell>
                    <TableCell className="text-xs">{fmt(o.updated_at)}</TableCell>
                  </TableRow>
                ))}
              </TableBody>
            </Table>
          </CardContent>
        </Card>
      )}

      {query && (
        <Card>
          <CardHeader>
            <div className="flex items-center justify-between">
              <CardTitle className="text-base">
                Courses{coursesData ? ` — Semester ${coursesData.semester} (${coursesData.courses.length})` : ""}
              </CardTitle>
              <Select value={courseNameLang} onValueChange={(v) => setCourseNameLang(v as "en" | "zh")}>
                <SelectTrigger className="w-32">
                  <SelectValue />
                </SelectTrigger>
                <SelectContent>
                  <SelectItem value="en">English</SelectItem>
                  <SelectItem value="zh">Chinese</SelectItem>
                </SelectContent>
              </Select>
            </div>
          </CardHeader>
          <CardContent>
            {!coursesData ? (
              <div className="flex items-center justify-center py-6 text-muted-foreground">
                <Loader2 className="mr-2 h-4 w-4 animate-spin" /> Loading…
              </div>
            ) : coursesData.courses.length === 0 ? (
              <div className="py-6 text-center text-muted-foreground">No courses</div>
            ) : (
            <Table>
              <TableHeader>
                <TableRow>
                  <TableHead>Color</TableHead>
                  <TableHead>Course Code</TableHead>
                  <TableHead>Name</TableHead>
                  <TableHead>Custom Names</TableHead>
                </TableRow>
              </TableHeader>
              <TableBody>
                {[...coursesData.courses].sort((a, b) => (a.course_no ?? "").localeCompare(b.course_no ?? "")).map((c) => {
                  const paletteLight = coursesData.palette_light ?? [];
                  const paletteDark = coursesData.palette_dark ?? [];
                  const overrideIdx = c.color_hex ? paletteLight.findIndex(
                    (p: string) => p.toLowerCase() === c.color_hex!.toLowerCase()
                  ) : -1;
                  const isPresetOverride = overrideIdx >= 0;
                  const isCustom = !!c.color_hex && !isPresetOverride;
                  return (
                  <TableRow key={c.id}>
                    <TableCell>
                      <div className="flex items-center gap-1">
                        {isPresetOverride ? (
                          <>
                            <div className="h-4 w-4 rounded border" style={{ backgroundColor: paletteLight[overrideIdx] }} title="Preset (light)" />
                            <div className="h-4 w-4 rounded border" style={{ backgroundColor: paletteDark[overrideIdx] }} title="Preset (dark)" />
                            <span className="font-mono text-xs">#{overrideIdx}</span>
                          </>
                        ) : isCustom ? (
                          <>
                            <div className="h-4 w-4 rounded border" style={{ backgroundColor: c.color_hex! }} title="Custom" />
                            <span className="font-mono text-xs">{c.color_hex}</span>
                          </>
                        ) : (
                          <>
                            <div className="h-4 w-4 rounded border" style={{ backgroundColor: c.default_color_light }} title="Default (light)" />
                            <div className="h-4 w-4 rounded border" style={{ backgroundColor: c.default_color_dark }} title="Default (dark)" />
                            <span className="font-mono text-xs text-muted-foreground">#{c.default_palette_index}</span>
                          </>
                        )}
                      </div>
                    </TableCell>
                    <TableCell className="font-mono text-xs">{c.client_course_no}</TableCell>
                    <TableCell className="text-xs max-w-[200px] truncate" title={`${c.course_name}${c.course_name_en ? ` / ${c.course_name_en}` : ""}`}>
                      {courseNameLang === "en" ? (c.course_name_en || c.course_name) : c.course_name}
                    </TableCell>
                    <TableCell className="text-xs max-w-[200px] truncate">
                      {(() => {
                        const names = typeof c.custom_names === "string" ? JSON.parse(c.custom_names) : c.custom_names;
                        return names && typeof names === "object" && Object.keys(names).length > 0
                          ? Object.entries(names).map(([lang, name]) => `${lang}: ${name}`).join(", ")
                          : "—";
                      })()}
                    </TableCell>
                  </TableRow>
                  );
                })}
              </TableBody>
            </Table>
            )}
          </CardContent>
        </Card>
      )}

      {query && (
        <Card>
          <CardHeader>
            <CardTitle className="text-base flex items-center gap-2">
              <RefreshCw className={`h-4 w-4 ${query ? "animate-spin" : ""}`} />
              Live Sync Log
            </CardTitle>
            <CardDescription>
              Auto-refreshes every 2s. Shows executor events, override PATCHes, and auth events.
            </CardDescription>
          </CardHeader>
          <CardContent>
            <div className="rounded-md border bg-muted/30 font-mono text-xs h-80 overflow-y-auto p-3 space-y-0.5">
              {logEntries.length === 0 && (
                <div className="text-muted-foreground text-center py-8">
                  Waiting for sync events...
                </div>
              )}
              {logEntries.map((e) => {
                const ts = new Date(e.ts).toLocaleTimeString();
                const levelColor =
                  e.level === "ERROR" ? "text-red-500" :
                  e.level === "WARN" ? "text-yellow-500" : "text-muted-foreground";
                return (
                  <div key={e.id} className="flex gap-2 leading-5">
                    <span className="text-muted-foreground shrink-0">{ts}</span>
                    <span className={`shrink-0 w-12 ${levelColor}`}>{e.level}</span>
                    <span className="shrink-0 text-blue-500 w-16">{e.source}</span>
                    {e.device_label && (
                      <span className="shrink-0 text-purple-400">
                        [{e.platform ?? "?"}/{e.device_label}]
                      </span>
                    )}
                    <span className="text-foreground">{e.message}</span>
                  </div>
                );
              })}
              <div ref={logEndRef} />
            </div>
          </CardContent>
        </Card>
      )}
    </Section>
  );
}

function RunStatusBadge({ status }: { status: string }) {
  const colors: Record<string, string> = {
    succeeded: "bg-green-500/10 text-green-600",
    running: "bg-blue-500/10 text-blue-600",
    pending: "bg-yellow-500/10 text-yellow-600",
    failed: "bg-red-500/10 text-red-600",
    disabled: "bg-gray-500/10 text-gray-600",
    cancelled: "bg-gray-500/10 text-gray-600",
    ignored: "bg-gray-500/10 text-gray-600",
    locally_completed: "bg-green-500/10 text-green-600",
    none: "bg-gray-500/10 text-gray-600",
  };
  return (
    <Badge variant="default" className={colors[status] ?? "bg-gray-500/10 text-gray-600"}>
      {status}
    </Badge>
  );
}

function CredentialBadge({ status }: { status: string }) {
  if (status === "active") {
    return (
      <Badge variant="default" className="bg-green-500/10 text-green-600">
        <CheckCircle2 className="mr-1 h-3 w-3" />
        Valid
      </Badge>
    );
  }
  return (
    <Badge variant="destructive">
      <AlertCircle className="mr-1 h-3 w-3" />
      {status}
    </Badge>
  );
}
