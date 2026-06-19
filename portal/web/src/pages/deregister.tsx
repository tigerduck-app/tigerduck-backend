import { useState } from "react";
import { useMutation } from "@tanstack/react-query";
import { toast } from "sonner";
import { AlertTriangle, UserMinus } from "lucide-react";
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

type DeregisterResult = {
  deleted: boolean;
  student_id: string;
  user_id: string;
};

export function DeregisterPage() {
  const [studentId, setStudentId] = useState("");
  const [confirmOpen, setConfirmOpen] = useState(false);

  const mutation = useMutation({
    mutationFn: () =>
      api<DeregisterResult>("/api/deregister", {
        method: "POST",
        json: { student_id: studentId.trim() },
      }),
    onSuccess: (r) => {
      toast.success(
        `Deregistered student ${r.student_id} (user ${r.user_id})`,
      );
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
