import { useState } from "react";
import { useMutation } from "@tanstack/react-query";
import { toast } from "sonner";
import { AlertTriangle, Download, UserMinus } from "lucide-react";
import { api, ApiError } from "@/lib/api";
import { Button } from "@/components/ui/button";
import { Card, CardContent } from "@/components/ui/card";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { PageHeader } from "@/components/ui/section";

type DeregisterReport = {
  deleted: boolean;
  timestamp: string;
  student_id: string;
  user_id: string;
  account_created_at: string | null;
  devices: {
    device_id: string;
    client_device_id: string;
    platform: string;
    app_version: string | null;
    os_version: string | null;
    last_seen_at: string | null;
  }[];
  deleted_counts: Record<string, number | string>;
};

function downloadReport(report: DeregisterReport) {
  const blob = new Blob([JSON.stringify(report, null, 2)], {
    type: "application/json",
  });
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url;
  a.download = `deregister-${report.student_id}-${new Date().toISOString().slice(0, 10)}.json`;
  a.click();
  URL.revokeObjectURL(url);
}

export function DeregisterPage() {
  const [studentId, setStudentId] = useState("");
  const [confirmOpen, setConfirmOpen] = useState(false);
  const [lastReport, setLastReport] = useState<DeregisterReport | null>(null);

  const mutation = useMutation({
    mutationFn: () =>
      api<DeregisterReport>("/api/deregister", {
        method: "POST",
        json: { student_id: studentId.trim() },
      }),
    onSuccess: (r) => {
      toast.success(
        `Deregistered student ${r.student_id} (user ${r.user_id})`,
      );
      setLastReport(r);
      setStudentId("");
      setConfirmOpen(false);
    },
    onError: (e) => {
      const msg =
        e instanceof ApiError
          ? e.status === 404
            ? "Student not found"
            : `HTTP ${e.status} — ${e.message}`
          : (e as Error).message;
      toast.error(msg);
      setConfirmOpen(false);
    },
  });

  const trimmed = studentId.trim();

  return (
    <div className="space-y-6">
      <PageHeader
        title="Deregister student"
        description="Permanently delete a student and all associated data (devices, sessions, credentials, sync state, push jobs, etc.). This cannot be undone."
      />

      <Card>
        <CardContent className="space-y-4">
          <div className="grid max-w-sm gap-1.5">
            <Label htmlFor="student-id">School ID</Label>
            <Input
              id="student-id"
              placeholder="B10000000"
              value={studentId}
              onChange={(e) => setStudentId(e.target.value)}
              maxLength={32}
              autoFocus
            />
          </div>
          <Button
            variant="destructive"
            disabled={!trimmed}
            onClick={() => setConfirmOpen(true)}
          >
            <UserMinus className="mr-2 h-4 w-4" />
            Deregister
          </Button>
        </CardContent>
      </Card>

      {lastReport && (
        <Card>
          <CardContent className="space-y-4">
            <div className="flex flex-wrap items-center justify-between gap-2">
              <h3 className="text-sm font-semibold">Deletion report</h3>
              <Button
                variant="outline"
                size="sm"
                onClick={() => downloadReport(lastReport)}
              >
                <Download className="mr-2 h-4 w-4" />
                Download JSON
              </Button>
            </div>

            <div className="rounded-md border bg-muted/50 p-4 text-sm space-y-3">
              <div className="grid grid-cols-2 gap-x-8 gap-y-1">
                <span className="text-muted-foreground">Student ID</span>
                <span className="font-mono">{lastReport.student_id}</span>
                <span className="text-muted-foreground">User ID</span>
                <span className="font-mono text-xs">{lastReport.user_id}</span>
                <span className="text-muted-foreground">Account created</span>
                <span>{lastReport.account_created_at ? new Date(lastReport.account_created_at).toLocaleString() : "—"}</span>
                <span className="text-muted-foreground">Deleted at</span>
                <span>{new Date(lastReport.timestamp).toLocaleString()}</span>
              </div>

              {lastReport.devices.length > 0 && (
                <div>
                  <h4 className="font-medium mb-1">Devices ({lastReport.devices.length})</h4>
                  <div className="space-y-1">
                    {lastReport.devices.map((d) => (
                      <div
                        key={d.device_id}
                        className="flex items-center gap-3 rounded bg-background px-2 py-1 text-xs"
                      >
                        <span className="font-mono text-muted-foreground shrink-0">
                          {d.platform}{d.os_version ? ` ${d.os_version}` : ""}
                        </span>
                        <span className="truncate">
                          {d.client_device_id}
                        </span>
                        <span className="ml-auto text-muted-foreground shrink-0">
                          {d.last_seen_at ? new Date(d.last_seen_at).toLocaleDateString() : "never"}
                        </span>
                      </div>
                    ))}
                  </div>
                </div>
              )}

              <div>
                <h4 className="font-medium mb-1">Deleted rows</h4>
                <div className="grid grid-cols-2 gap-x-8 gap-y-0.5 text-xs">
                  {Object.entries(lastReport.deleted_counts)
                    .filter(([, v]) => v !== 0 && v !== "table_not_found")
                    .map(([table, count]) => (
                      <div key={table} className="contents">
                        <span className="text-muted-foreground">{table}</span>
                        <span className="font-mono">{count}</span>
                      </div>
                    ))}
                </div>
              </div>
            </div>
          </CardContent>
        </Card>
      )}

      <Dialog open={confirmOpen} onOpenChange={setConfirmOpen}>
        <DialogContent>
          <DialogHeader>
            <DialogTitle className="flex items-center gap-2">
              <AlertTriangle className="h-5 w-5 text-destructive" />
              Confirm deregistration
            </DialogTitle>
            <DialogDescription>
              Are you sure you want to deregister student{" "}
              <span className="font-mono font-semibold text-foreground">
                {trimmed}
              </span>
              ? This will permanently delete all data.
            </DialogDescription>
          </DialogHeader>
          <DialogFooter>
            <Button
              variant="outline"
              onClick={() => setConfirmOpen(false)}
              disabled={mutation.isPending}
            >
              Cancel
            </Button>
            <Button
              variant="destructive"
              disabled={mutation.isPending}
              onClick={() => mutation.mutate()}
            >
              {mutation.isPending
                ? "Deregistering…"
                : "Yes, permanently delete"}
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </div>
  );
}
