// The data-inspection tab: pick a student, then explore their sync state.

import { useEffect, useRef, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { Loader2, Play, RefreshCw, Search } from "lucide-react";
import { Button } from "@/components/ui/button";
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { Section } from "@/components/ui/section";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";
import { NodeDetailPanel } from "./NodeDetailPanel";
import { TopologyOverview } from "./TopologyOverview";
import { RunStatusBadge, fmt } from "./format";
import type { LogEntry, LogsResponse, SyncCoursesResponse, SyncEventsResponse, TopologyNode } from "./types";

export function SyncTab() {
  const [studentId, setStudentId] = useState(() => {
    try { return localStorage.getItem("sync-student-id") ?? ""; } catch { return ""; }
  });
  const [query, setQuery] = useState(() => {
    try { return localStorage.getItem("sync-query") ?? ""; } catch { return ""; }
  });
  const [logEntries, setLogEntries] = useState<LogEntry[]>([]);
  const [latestId, setLatestId] = useState(0);
  const [courseNameLang, setCourseNameLang] = useState<"en" | "zh">("en");
  const [selectedNode, setSelectedNode] = useState<TopologyNode | null>(null);
  const logEndRef = useRef<HTMLDivElement>(null);
  const logContainerRef = useRef<HTMLDivElement>(null);

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

  const [newLogIds, setNewLogIds] = useState<Set<number>>(new Set());
  const isInitialLoad = useRef(true);
  useEffect(() => {
    const d = logsQuery.data;
    if (d && d.entries.length > 0) {
      const container = logContainerRef.current;
      const wasAtBottom = container
        ? container.scrollHeight - container.scrollTop - container.clientHeight < 40
        : false;

      if (!isInitialLoad.current) {
        const ids = new Set(d.entries.map((e) => e.id));
        setNewLogIds((prev) => new Set([...prev, ...ids]));
        setTimeout(() => {
          setNewLogIds((prev) => {
            const next = new Set(prev);
            ids.forEach((id) => next.delete(id));
            return next;
          });
        }, 2000);
        setLogEntries((prev) => [...prev, ...d.entries].slice(-500));
        setLatestId(d.latest_id);
        if (wasAtBottom && logContainerRef.current) {
          setTimeout(() => {
            const c = logContainerRef.current;
            if (c) c.scrollTop = c.scrollHeight;
          }, 50);
        }
      }
      isInitialLoad.current = false;
      setLogEntries((prev) => [...prev, ...d.entries].slice(-500));
      setLatestId(d.latest_id);
      setTimeout(() => {
        const c = logContainerRef.current;
        if (c) c.scrollTop = c.scrollHeight;
      }, 50);
    }
  }, [logsQuery.data]);

  const handleSearch = () => {
    const trimmed = studentId.trim();
    if (trimmed) {
      setQuery(trimmed);
      try { localStorage.setItem("sync-student-id", trimmed); localStorage.setItem("sync-query", trimmed); } catch {}
      setLogEntries([]);
      setLatestId(0);
      isInitialLoad.current = true;
      setSelectedNode(null);
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
      {/* 1. Search card */}
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
            Student &quot;{data.student_id}&quot; not found
          </CardContent>
        </Card>
      )}

      {query && data?.found !== false && (
      <div className="grid grid-cols-1 xl:grid-cols-2 gap-4">
      {/* ===== LEFT COLUMN: Topology + Details ===== */}
      <div className="space-y-4">
      {/* 2. Topology overview */}
      {data?.found && (
        <TopologyOverview
          topology={data.topology}
          pollStatus={data.poll_status}
          devices={data.devices ?? []}
          pushJobs={data.push_jobs}
          selectedNode={selectedNode}
          onSelectNode={setSelectedNode}
        />
      )}

      {/* 3. Detail panel */}
      {data?.found && selectedNode && (
        <NodeDetailPanel
          selectedNode={selectedNode}
          data={data}
          coursesData={coursesData}
          courseNameLang={courseNameLang}
          setCourseNameLang={setCourseNameLang}
          studentId={query}
        />
      )}
      </div>

      {/* ===== RIGHT COLUMN: Jobs + History + Log ===== */}
      <div className="space-y-4">
      {/* 4. Sync Jobs card */}
        <Card>
          <CardHeader>
            <CardTitle className="text-base">Sync Jobs</CardTitle>
          </CardHeader>
          <CardContent>
            {!data ? (
              <div className="flex items-center justify-center py-6 text-muted-foreground">
                <Loader2 className="mr-2 h-4 w-4 animate-spin" /> Loading...
              </div>
            ) : (
            <div className="overflow-x-auto"><Table>
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
            </Table></div>
            )}
          </CardContent>
        </Card>

      {/* 5. Run History card */}
        <Card>
          <CardHeader>
            <CardTitle className="text-base">Run History{data?.runs ? ` (${data.runs.length})` : ""}</CardTitle>
          </CardHeader>
          <CardContent>
            {!data ? (
              <div className="flex items-center justify-center py-6 text-muted-foreground">
                <Loader2 className="mr-2 h-4 w-4 animate-spin" /> Loading...
              </div>
            ) : (
            <div className="overflow-x-auto"><Table>
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
            </Table></div>
            )}
          </CardContent>
        </Card>

      {/* 6. Live Sync Log card */}
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
            <div ref={logContainerRef} className="rounded-md border bg-muted/30 font-mono text-xs h-80 overflow-y-auto p-3 space-y-0.5">
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
                  <div key={e.id} className={`flex gap-2 leading-5 transition-colors duration-1000 ${newLogIds.has(e.id) ? "bg-yellow-500/20 rounded px-1 -mx-1" : ""}`}>
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
      </div>
      </div>
      )}
    </Section>
  );
}
