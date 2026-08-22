// Manual Moodle operations — trigger a sync, pause the scheduler, and
// the confirmations those need.

import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { toast } from "sonner";
import { CheckCircle2, Pause, Play, RefreshCw } from "lucide-react";
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
import { Section } from "@/components/ui/section";
import { JobsTable } from "./JobsTable";
import { SyncStats } from "./SyncStats";
import { fmt } from "./format";
import type { MoodleStatus } from "./types";

export function ActionsTab() {
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

  const retryAllMut = useMutation({
    mutationFn: () =>
      fetch("/api/moodle/retry-all-jobs", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ notify: notifyOnRetry }),
      }).then((r) => r.json()),
    onSuccess: (data: { retried: number; notified: number }) => {
      const msg = data.notified
        ? `Re-queued ${data.retried} job(s), notified ${data.notified} user(s)`
        : `Re-queued ${data.retried} job(s)`;
      toast.success(msg);
      qc.invalidateQueries({ queryKey: ["moodle-status"] });
      qc.invalidateQueries({ queryKey: ["moodle-students"] });
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
            <div className="flex flex-wrap items-center gap-2">
              <Button
                size="sm"
                variant="outline"
                onClick={() => retryAllMut.mutate()}
                disabled={retryAllMut.isPending}
              >
                <RefreshCw className="mr-1.5 h-3.5 w-3.5" />
                Retry all
              </Button>
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

      <SyncStats />
      <JobsTable />
    </Section>
  );
}
