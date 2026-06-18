import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { toast } from "sonner";
import {
  AlertCircle,
  CheckCircle2,
  Clock,
  Pause,
  Play,
  RefreshCw,
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
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
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

type SyncJob = {
  id: number;
  student_id: string;
  job_type: string;
  status: string;
  attempts: number;
  last_success_at: string | null;
  last_failure_at: string | null;
  last_error: string | null;
  run_after: string;
  created_at: string;
};

function fmt(iso: string | null): string {
  if (!iso) return "—";
  return new Date(iso).toLocaleString();
}

export function MoodlePage() {
  const qc = useQueryClient();
  const [hours, setHours] = useState("4");
  const [statusFilter, setStatusFilter] = useState<string>("all");

  const status = useQuery<MoodleStatus>({
    queryKey: ["moodle-status"],
    queryFn: () => fetch("/api/moodle/status").then((r) => r.json()),
    refetchInterval: 10_000,
  });

  const jobs = useQuery<SyncJob[]>({
    queryKey: ["moodle-jobs", statusFilter],
    queryFn: () => {
      const params = new URLSearchParams();
      if (statusFilter !== "all") params.set("status", statusFilter);
      return fetch(`/api/moodle/jobs?${params}`).then((r) => r.json());
    },
    refetchInterval: 15_000,
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
      fetch("/api/moodle/retry-all", { method: "POST" }).then((r) => r.json()),
    onSuccess: (data: { retried: number }) => {
      toast.success(`Retried ${data.retried} job(s)`);
      qc.invalidateQueries({ queryKey: ["moodle-jobs"] });
      qc.invalidateQueries({ queryKey: ["moodle-status"] });
    },
  });

  const isSuspended = !!status.data?.suspended_until;
  const c = status.data?.job_counts;

  return (
    <>
      <PageHeader
        title="Moodle Sync"
        description="Manage server-side Moodle sync jobs"
      />

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
          <CardContent>
            <div className="flex flex-wrap items-center gap-3">
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
              <div className="ml-auto flex items-center gap-2">
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
                <Button
                  size="sm"
                  variant="outline"
                  onClick={() => retryMut.mutate()}
                  disabled={retryMut.isPending}
                >
                  <RefreshCw className="mr-1.5 h-3.5 w-3.5" />
                  Retry all failed
                </Button>
              </div>
            </div>
          </CardContent>
        </Card>
      </Section>

      <Section>
        <Card>
          <CardHeader>
            <CardTitle>Sync Jobs</CardTitle>
            <CardDescription>
              <div className="flex items-center gap-2">
                <span>Filter:</span>
                <Select value={statusFilter} onValueChange={setStatusFilter}>
                  <SelectTrigger className="w-32">
                    <SelectValue />
                  </SelectTrigger>
                  <SelectContent>
                    <SelectItem value="all">All</SelectItem>
                    <SelectItem value="pending">Pending</SelectItem>
                    <SelectItem value="running">Running</SelectItem>
                    <SelectItem value="failed">Failed</SelectItem>
                    <SelectItem value="disabled">Disabled</SelectItem>
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
                  <TableHead>Job type</TableHead>
                  <TableHead>Status</TableHead>
                  <TableHead>Attempts</TableHead>
                  <TableHead>Last success</TableHead>
                  <TableHead>Last error</TableHead>
                  <TableHead>Next run</TableHead>
                </TableRow>
              </TableHeader>
              <TableBody>
                {jobs.data?.map((j) => (
                  <TableRow key={j.id}>
                    <TableCell className="font-mono text-xs">
                      {j.student_id}
                    </TableCell>
                    <TableCell>
                      <Badge variant="outline" className="text-xs">
                        {j.job_type}
                      </Badge>
                    </TableCell>
                    <TableCell>
                      <StatusBadge status={j.status} />
                    </TableCell>
                    <TableCell>{j.attempts}</TableCell>
                    <TableCell className="text-xs">
                      {fmt(j.last_success_at)}
                    </TableCell>
                    <TableCell className="max-w-[200px] truncate text-xs text-destructive">
                      {j.last_error ?? "—"}
                    </TableCell>
                    <TableCell className="text-xs">
                      {fmt(j.run_after)}
                    </TableCell>
                  </TableRow>
                ))}
                {jobs.data?.length === 0 && (
                  <TableRow>
                    <TableCell colSpan={7} className="text-center text-muted-foreground">
                      No jobs found
                    </TableCell>
                  </TableRow>
                )}
              </TableBody>
            </Table>
          </CardContent>
        </Card>
      </Section>
    </>
  );
}

function StatusBadge({ status }: { status: string }) {
  switch (status) {
    case "pending":
      return (
        <Badge variant="default" className="bg-blue-500/10 text-blue-600">
          <Clock className="mr-1 h-3 w-3" />
          Pending
        </Badge>
      );
    case "running":
      return (
        <Badge variant="default" className="bg-green-500/10 text-green-600">
          <RefreshCw className="mr-1 h-3 w-3 animate-spin" />
          Running
        </Badge>
      );
    case "failed":
      return (
        <Badge variant="destructive">
          <AlertCircle className="mr-1 h-3 w-3" />
          Failed
        </Badge>
      );
    case "disabled":
      return (
        <Badge variant="outline" className="text-muted-foreground">
          <Pause className="mr-1 h-3 w-3" />
          Disabled
        </Badge>
      );
    default:
      return <Badge variant="outline">{status}</Badge>;
  }
}
