import { useEffect, useRef, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
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
  return (
    <>
      <PageHeader
        title="Moodle Sync"
        description="Manage server-side Moodle sync jobs"
      />
      <Tabs defaultValue="actions" className="space-y-4">
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
    </Section>
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
  client_course_no: string;
  source: string;
  color_hex: string | null;
  is_hidden: boolean | null;
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

type SyncEventsResponse = {
  student_id: string;
  found: boolean;
  jobs?: SyncJob[];
  runs?: SyncRun[];
  overrides?: SyncOverride[];
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

function SyncTab() {
  const [studentId, setStudentId] = useState("");
  const [query, setQuery] = useState("");
  const [logEntries, setLogEntries] = useState<LogEntry[]>([]);
  const [latestId, setLatestId] = useState(0);
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

      {data?.found && data.jobs && (
        <Card>
          <CardHeader>
            <CardTitle className="text-base">Sync Jobs</CardTitle>
          </CardHeader>
          <CardContent>
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
                {data.jobs.map((j) => (
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
                {data.jobs.length === 0 && (
                  <TableRow><TableCell colSpan={7} className="text-center text-muted-foreground">No sync jobs</TableCell></TableRow>
                )}
              </TableBody>
            </Table>
          </CardContent>
        </Card>
      )}

      {data?.found && data.runs && (
        <Card>
          <CardHeader>
            <CardTitle className="text-base">Run History ({data.runs.length})</CardTitle>
          </CardHeader>
          <CardContent>
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
                {data.runs.map((r) => {
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
                {data.runs.length === 0 && (
                  <TableRow><TableCell colSpan={7} className="text-center text-muted-foreground">No sync runs yet</TableCell></TableRow>
                )}
              </TableBody>
            </Table>
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

      {coursesData && coursesData.courses.length > 0 && (
        <Card>
          <CardHeader>
            <CardTitle className="text-base">
              Courses — Semester {coursesData.semester} ({coursesData.courses.length})
            </CardTitle>
          </CardHeader>
          <CardContent>
            <Table>
              <TableHeader>
                <TableRow>
                  <TableHead>Color</TableHead>
                  <TableHead>Course Code</TableHead>
                  <TableHead>Name</TableHead>
                  <TableHead>Hidden</TableHead>
                  <TableHead>Custom Names</TableHead>
                </TableRow>
              </TableHeader>
              <TableBody>
                {coursesData.courses.map((c) => {
                  const isCustom = !!c.color_hex;
                  return (
                  <TableRow key={c.id}>
                    <TableCell>
                      <div className="flex items-center gap-1">
                        {isCustom ? (
                          <>
                            <div className="h-4 w-4 rounded border" style={{ backgroundColor: c.color_hex! }} title="Custom" />
                            <span className="font-mono text-xs">{c.color_hex}</span>
                          </>
                        ) : (
                          <>
                            <div className="h-4 w-4 rounded border" style={{ backgroundColor: c.default_color_light }} title="iOS / Android light" />
                            <div className="h-4 w-4 rounded border" style={{ backgroundColor: c.default_color_dark }} title="Android dark" />
                            <span className="font-mono text-xs text-muted-foreground">#{c.default_palette_index}</span>
                          </>
                        )}
                      </div>
                    </TableCell>
                    <TableCell className="font-mono text-xs">{c.client_course_no}</TableCell>
                    <TableCell className="text-xs max-w-[200px] truncate" title={c.course_name}>
                      {c.course_name.replace(/^\d+\.\d【[^】]+】\s*\S+\s*/, "")}
                    </TableCell>
                    <TableCell>
                      {c.is_hidden ? <Badge variant="destructive">hidden</Badge> : "—"}
                    </TableCell>
                    <TableCell className="text-xs max-w-[200px] truncate">
                      {c.custom_names && Object.keys(c.custom_names).length > 0
                        ? Object.entries(c.custom_names).map(([lang, name]) => `${lang}: ${name}`).join(", ")
                        : "—"}
                    </TableCell>
                  </TableRow>
                  );
                })}
              </TableBody>
            </Table>
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
                      <span className="shrink-0 text-purple-400 truncate max-w-[80px]" title={e.device_label}>
                        [{e.platform ?? "?"}/{e.device_label.slice(0, 8)}]
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
