// The global sync-job queue.

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
import { fmt, statusBadge } from "./format";
import type { GlobalSyncJob } from "./types";

export function JobsTable() {
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
        <div className="overflow-x-auto"><Table>
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
        </Table></div>
      </CardContent>
    </Card>
  );
}
