import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { toast } from "sonner";
import {
  AlertCircle,
  CheckCircle2,
  Pause,
  Play,
  RefreshCw,
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
        </TabsList>
        <TabsContent value="actions">
          <ActionsTab />
        </TabsContent>
        <TabsContent value="lists">
          <ListsTab />
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
