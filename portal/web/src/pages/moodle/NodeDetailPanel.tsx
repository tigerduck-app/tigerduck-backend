// Detail panel for whichever topology node is selected. One component per
// node kind would be the next step; today it is a single switch, kept
// together because the kinds share their table and empty-state chrome.

import { Fragment, useMemo, useState } from "react";
import { Check, CloudOff, Loader2, Minus, Server } from "lucide-react";
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
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";
import { RunStatusBadge, deviceLabel, fmt, platformIcon, platformLabel, relativeTime, timeUntil } from "./format";
import type { QueuedJob, SyncCoursesResponse, SyncDevice, SyncEventsResponse, TopologyNode } from "./types";

/**
 * Split rows into one section per NTUST term, newest first. Rows whose term
 * could not be determined collect under a trailing heading of their own
 * rather than being dropped or filed under a term they may not belong to.
 */
function groupBySemester<T>(rows: T[], termOf: (row: T) => string | null | undefined) {
  const byTerm = new Map<string, T[]>();
  for (const row of rows) {
    const term = termOf(row) || "";
    const bucket = byTerm.get(term);
    if (bucket) bucket.push(row);
    else byTerm.set(term, [row]);
  }
  return [...byTerm.entries()]
    // Unknown last, real terms newest first.
    .sort(([a], [b]) => (a ? 0 : 1) - (b ? 0 : 1) || b.localeCompare(a))
    .map(([semester, items]) => ({ semester, items }));
}

function SemesterHeadingRow({
  semester,
  count,
  colSpan,
  current,
}: {
  semester: string;
  count: string;
  colSpan: number;
  current?: boolean;
}) {
  return (
    <TableRow className="bg-muted/50 hover:bg-muted/50">
      <TableCell colSpan={colSpan} className="py-1.5">
        <div className="flex items-center gap-2">
          <span className="font-mono text-xs font-medium">
            {semester || "Unknown semester"}
          </span>
          {current && (
            <Badge variant="outline" className="text-[10px] px-1 py-0">
              Current
            </Badge>
          )}
          <span className="text-xs text-muted-foreground">{count}</span>
        </div>
      </TableCell>
    </TableRow>
  );
}

export function NodeDetailPanel({
  selectedNode,
  data,
  coursesData,
  courseNameLang,
  setCourseNameLang,
  studentId,
}: {
  selectedNode: TopologyNode;
  data: SyncEventsResponse;
  coursesData?: SyncCoursesResponse;
  courseNameLang: "en" | "zh";
  setCourseNameLang: (v: "en" | "zh") => void;
  studentId: string;
}) {
  // Clients reconcile every semester against the backend, so the courses
  // response spans terms. Everything is shown by default and grouped under a
  // heading per term — silently narrowing to one term is what made rows look
  // lost. The filter narrows on demand; it is not the default.
  const [semesterFilter, setSemesterFilter] = useState<string>("__all__");
  const courseSemesters = useMemo(() => {
    const fromRows = [
      ...(coursesData?.courses ?? []).map((c) => c.semester),
      ...(coursesData?.assignments ?? []).map((a) => a.semester),
    ].filter((s): s is string => !!s);
    return [...new Set(fromRows.concat(coursesData?.semesters ?? []))].sort().reverse();
  }, [coursesData]);
  const activeSemester =
    semesterFilter === "__focus__" ? coursesData?.semester ?? "" : semesterFilter;
  const visibleCourses = useMemo(() => {
    const all = coursesData?.courses ?? [];
    if (semesterFilter === "__all__") return all;
    if (!activeSemester) return all;
    return all.filter((c) => (c.semester ?? "") === activeSemester);
  }, [coursesData, semesterFilter, activeSemester]);
  const visibleTombstones = useMemo(() => {
    const all = coursesData?.tombstones ?? [];
    if (semesterFilter === "__all__" || !activeSemester) return all;
    return all.filter((t) => t.semester === activeSemester);
  }, [coursesData, semesterFilter, activeSemester]);
  const visibleAssignments = useMemo(() => {
    const all = coursesData?.assignments ?? [];
    if (semesterFilter === "__all__" || !activeSemester) return all;
    return all.filter((a) => (a.semester ?? "") === activeSemester);
  }, [coursesData, semesterFilter, activeSemester]);
  // One section per term, newest first, each with its own deletions — a term
  // with a tombstone and no live rows still gets a heading, which is exactly
  // the case someone opens this panel to look at.
  const semesterGroups = useMemo(() => {
    const terms = [
      ...new Set([
        ...visibleCourses.map((c) => c.semester ?? ""),
        ...visibleTombstones.map((t) => t.semester ?? ""),
      ]),
    ].sort().reverse();
    return terms.map((term) => ({
      semester: term,
      courses: visibleCourses
        .filter((c) => (c.semester ?? "") === term)
        .sort((a, b) => (a.course_no ?? "").localeCompare(b.course_no ?? "")),
      tombstones: visibleTombstones.filter((t) => (t.semester ?? "") === term),
    }));
  }, [visibleCourses, visibleTombstones]);
  if (selectedNode.kind === "backend") {
    return (
      <Card>
        <CardHeader>
          <div className="flex items-center justify-between">
            <CardTitle className="text-base flex items-center gap-2">
              <Server className="h-4 w-4 text-blue-500" />
              Backend Details
            </CardTitle>
          </div>
        </CardHeader>
        <CardContent>
          <Tabs defaultValue="courses">
            <TabsList>
              <TabsTrigger value="courses">
                Courses{coursesData ? ` (${visibleCourses.length}${semesterFilter !== "__all__" && coursesData.courses.length !== visibleCourses.length ? ` of ${coursesData.courses.length}` : ""}${visibleTombstones.length ? ` + ${visibleTombstones.length} deleted` : ""})` : ""}
              </TabsTrigger>
              <TabsTrigger value="assignments">
                Assignments{coursesData?.assignments ? ` (${coursesData.assignments.length})` : ""}
              </TabsTrigger>
              <TabsTrigger value="overrides">
                Overrides{data.overrides ? ` (${data.overrides.length})` : ""}
              </TabsTrigger>
            </TabsList>
            <TabsContent value="courses">
              {!coursesData ? (
                <div className="flex items-center justify-center py-6 text-muted-foreground">
                  <Loader2 className="mr-2 h-4 w-4 animate-spin" /> Loading...
                </div>
              ) : coursesData.courses.length === 0 ? (
                <div className="py-6 text-center text-muted-foreground">No courses</div>
              ) : visibleCourses.length === 0 && visibleTombstones.length === 0 ? (
                <div className="py-6 text-center text-muted-foreground">
                  No courses in {activeSemester || "this semester"} — {coursesData.courses.length} in other semesters.
                </div>
              ) : (
                <div className="space-y-2">
                <div className="flex justify-end gap-2">
                  <Select value={semesterFilter} onValueChange={setSemesterFilter}>
                    <SelectTrigger className="w-36 h-7 text-xs">
                      <SelectValue />
                    </SelectTrigger>
                    <SelectContent>
                      <SelectItem value="__all__">All semesters</SelectItem>
                      <SelectItem value="__focus__">
                        {coursesData.semester ? `Current (${coursesData.semester})` : "Current"}
                      </SelectItem>
                      {courseSemesters.map((sem) => (
                        <SelectItem key={sem} value={sem}>{sem}</SelectItem>
                      ))}
                    </SelectContent>
                  </Select>
                  <Select value={courseNameLang} onValueChange={(v) => setCourseNameLang(v as "en" | "zh")}>
                    <SelectTrigger className="w-28 h-7 text-xs">
                      <SelectValue />
                    </SelectTrigger>
                    <SelectContent>
                      <SelectItem value="en">English</SelectItem>
                      <SelectItem value="zh">Chinese</SelectItem>
                    </SelectContent>
                  </Select>
                </div>
                <div className="overflow-x-auto">
                  <Table>
                    <TableHeader>
                      <TableRow>
                        <TableHead>Color</TableHead>
                        <TableHead>Course Code</TableHead>
                        <TableHead>Name</TableHead>
                        <TableHead>Custom Names</TableHead>
                        <TableHead>Device</TableHead>
                      </TableRow>
                    </TableHeader>
                    <TableBody>
                      {semesterGroups.map((group) => (
                        <Fragment key={group.semester || "unfiled"}>
                          <TableRow className="bg-muted/50 hover:bg-muted/50">
                            <TableCell colSpan={5} className="py-1.5">
                              <div className="flex items-center gap-2">
                                <span className="font-mono text-xs font-medium">
                                  {group.semester || "No semester"}
                                </span>
                                {group.semester === coursesData.semester && (
                                  <Badge variant="outline" className="text-[10px] px-1 py-0">
                                    Current
                                  </Badge>
                                )}
                                <span className="text-xs text-muted-foreground">
                                  {group.courses.length} course{group.courses.length === 1 ? "" : "s"}
                                  {group.tombstones.length ? ` · ${group.tombstones.length} deleted` : ""}
                                </span>
                              </div>
                            </TableCell>
                          </TableRow>
                          {group.courses.map((c) => {
                            const paletteLight = coursesData.palette_light ?? [];
                            const paletteDark = coursesData.palette_dark ?? [];
                            const overrideIdx = c.color_hex
                              ? paletteLight.findIndex(
                                  (p: string) => p.toLowerCase() === c.color_hex!.toLowerCase()
                                )
                              : -1;
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
                                <TableCell>
                                  {c.updated_by_device_id ? (
                                    <Badge variant="secondary" className="text-[10px] font-normal">
                                      {deviceLabel(c.updated_by_device_id, data.devices ?? [])}
                                    </Badge>
                                  ) : (
                                    <span className="text-xs text-muted-foreground">—</span>
                                  )}
                                </TableCell>
                              </TableRow>
                            );
                          })}
                          {group.tombstones.map((t) => (
                            <TableRow key={`tomb-${t.course_key}`} className="opacity-50">
                              <TableCell>
                                <Badge variant="destructive" className="text-[10px] px-1 py-0">Deleted</Badge>
                              </TableCell>
                              <TableCell className="font-mono text-xs line-through">{t.course_no}</TableCell>
                              <TableCell className="text-xs text-muted-foreground">deleted {relativeTime(t.deleted_at)}</TableCell>
                              <TableCell className="text-xs text-muted-foreground">—</TableCell>
                              <TableCell>
                                {t.deleted_by_device_id ? (
                                  <Badge variant="secondary" className="text-[10px] font-normal">
                                    {deviceLabel(t.deleted_by_device_id, data.devices ?? [])}
                                  </Badge>
                                ) : (
                                  <span className="text-xs text-muted-foreground">—</span>
                                )}
                              </TableCell>
                            </TableRow>
                          ))}
                        </Fragment>
                      ))}
                    </TableBody>
                  </Table>
                </div>
                </div>
              )}
            </TabsContent>
            <TabsContent value="assignments">
              {!coursesData ? (
                <div className="flex items-center justify-center py-6 text-muted-foreground">
                  <Loader2 className="mr-2 h-4 w-4 animate-spin" /> Loading...
                </div>
              ) : !coursesData.assignments || coursesData.assignments.length === 0 ? (
                <div className="py-6 text-center text-muted-foreground">No assignments</div>
              ) : (
                <div className="overflow-x-auto">
                  <Table>
                    <TableHeader>
                      <TableRow>
                        <TableHead>Title</TableHead>
                        <TableHead>Course</TableHead>
                        <TableHead>Due</TableHead>
                        <TableHead>Submitted</TableHead>
                        <TableHead>Status</TableHead>
                      </TableRow>
                    </TableHeader>
                    <TableBody>
                      {coursesData.assignments.map((a) => {
                        const override = (data.overrides ?? []).find((o) => o.moodle_assignment_id === a.moodle_assignment_id);
                        return (
                        <TableRow key={a.id}>
                          <TableCell className="text-xs max-w-[200px] truncate" title={a.title}>{a.title}</TableCell>
                          <TableCell className="font-mono text-xs">{a.client_course_no || a.course_no || "—"}</TableCell>
                          <TableCell className="text-xs">{fmt(a.due_at)}</TableCell>
                          <TableCell className="text-center">
                            {a.provider_is_submitted ? (
                              <Check className="h-4 w-4 text-green-500 inline-block" />
                            ) : (
                              <Minus className="h-4 w-4 text-muted-foreground inline-block" />
                            )}
                          </TableCell>
                          <TableCell>{override ? <RunStatusBadge status={override.local_status} /> : <span className="text-xs text-muted-foreground">normal</span>}</TableCell>
                        </TableRow>
                        );
                      })}
                    </TableBody>
                  </Table>
                </div>
              )}
            </TabsContent>
            <TabsContent value="overrides">
              {data.overrides && data.overrides.length > 0 ? (
                <div className="overflow-x-auto">
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
                </div>
              ) : (
                <div className="py-6 text-center text-muted-foreground">No overrides</div>
              )}
            </TabsContent>
          </Tabs>
        </CardContent>
      </Card>
    );
  }

  // Device selected — use fresh data from the latest poll, not the stale snapshot in selectedNode
  const device = (data.devices ?? []).find((d) => d.id === selectedNode.device.id) ?? selectedNode.device;
  const deviceDeliveries = (data.push_deliveries ?? []).filter(
    (d) => d.device_id === device.id
  );
  const devicePushJobs = (data.push_jobs ?? []).filter(
    (pj) => pj.source_device_id === device.id
  );
  const deviceQueue = (data.queued_jobs ?? []).filter((j) =>
    j.recipients.some((r) => r.device_id === device.id)
  );

  const allOverrides = data.overrides ?? [];

  return (
    <Card>
      <CardHeader>
        <CardTitle className="text-base flex items-center gap-2">
          {platformIcon(device.platform)}
          {platformLabel(device.platform)}
          <span className="font-mono text-xs text-muted-foreground">
            {device.client_device_id.slice(0, 12)}
          </span>
          {device.cloud_sync_enabled === false && <Badge variant="secondary" className="text-[10px] px-1.5 py-0 gap-0.5 text-muted-foreground"><CloudOff className="h-2.5 w-2.5" />local</Badge>}
        </CardTitle>
        <CardDescription>
          {device.app_version && `v${device.app_version}`}
          {device.os_version && ` on ${platformLabel(device.platform)} ${device.os_version}`}
          {device.last_seen_at && ` · Last seen ${relativeTime(device.last_seen_at)}`}
        </CardDescription>
      </CardHeader>
      <CardContent>
        <Tabs defaultValue="data">
          <TabsList>
            <TabsTrigger value="data">Data</TabsTrigger>
            <TabsTrigger value="synced-list">Synced List</TabsTrigger>
            <TabsTrigger value="push">
              Push{deviceDeliveries.length > 0 ? ` (${deviceDeliveries.length})` : ""}
            </TabsTrigger>
            <TabsTrigger value="queued">
              Queued Jobs{deviceQueue.length > 0 ? ` (${deviceQueue.length})` : ""}
            </TabsTrigger>
            <TabsTrigger value="source-push">
              Source Jobs{devicePushJobs.length > 0 ? ` (${devicePushJobs.length})` : ""}
            </TabsTrigger>
            <TabsTrigger value="info">Info</TabsTrigger>
          </TabsList>
          <TabsContent value="data">
            {device.cloud_sync_enabled === false && (
              <div className="flex items-center gap-3 rounded-md border border-border p-3 my-2">
                <div className="h-2.5 w-2.5 rounded-full shrink-0 bg-muted-foreground/30" />
                <div className="min-w-0">
                  <div className="text-sm font-medium">Course sync is off on this device</div>
                  <div className="text-xs text-muted-foreground">
                    What follows is the account's data from its other devices; this one keeps its own copy locally.
                  </div>
                </div>
              </div>
            )}
            {!coursesData ? (
              <div className="flex items-center justify-center py-6 text-muted-foreground">
                <Loader2 className="mr-2 h-4 w-4 animate-spin" /> Loading...
              </div>
            ) : (
              <Tabs defaultValue="courses">
                <div className="mb-2 flex flex-wrap items-center justify-between gap-2">
                  <TabsList className="flex-wrap h-auto">
                    <TabsTrigger value="courses">Courses ({visibleCourses.length})</TabsTrigger>
                    <TabsTrigger value="custom-names">Custom Names ({visibleCourses.filter((c) => { const n = typeof c.custom_names === "string" ? JSON.parse(c.custom_names || "{}") : c.custom_names; return n && typeof n === "object" && Object.keys(n).length > 0; }).length})</TabsTrigger>
                    <TabsTrigger value="assignments">Assignments ({visibleAssignments.length})</TabsTrigger>
                    <TabsTrigger value="holiday-overrides">Holiday Overrides ({(coursesData.holiday_overrides ?? []).length})</TabsTrigger>
                    <TabsTrigger value="skipped-dates">Skipped Dates ({(coursesData.course_skipped_dates ?? []).length})</TabsTrigger>
                    <TabsTrigger value="settings">Settings ({(coursesData.settings_documents ?? []).length})</TabsTrigger>
                    <TabsTrigger value="bulletins">Bulletins ({(coursesData.bulletin_subscriptions ?? []).filter((s) => s.device_id === device.id).length})</TabsTrigger>
                  </TabsList>
                  <Select value={semesterFilter} onValueChange={setSemesterFilter}>
                    <SelectTrigger className="w-40 h-7 text-xs shrink-0">
                      <SelectValue />
                    </SelectTrigger>
                    <SelectContent>
                      <SelectItem value="__all__">All semesters</SelectItem>
                      <SelectItem value="__focus__">
                        {coursesData.semester ? `Current (${coursesData.semester})` : "Current"}
                      </SelectItem>
                      {courseSemesters.map((sem) => (
                        <SelectItem key={sem} value={sem}>{sem}</SelectItem>
                      ))}
                    </SelectContent>
                  </Select>
                </div>

                <TabsContent value="courses">
                  {visibleCourses.length === 0 ? (
                    <p className="text-sm text-muted-foreground py-4">
                      {activeSemester && (coursesData?.courses ?? []).length > 0
                        ? `No courses in ${activeSemester} — ${(coursesData?.courses ?? []).length} in other semesters.`
                        : "No courses"}
                    </p>
                  ) : (
                    <div className="space-y-2">
                      <div className="flex justify-end">
                        <Select value={courseNameLang} onValueChange={(v) => setCourseNameLang(v as "en" | "zh")}>
                          <SelectTrigger className="w-28 h-7 text-xs">
                            <SelectValue />
                          </SelectTrigger>
                          <SelectContent>
                            <SelectItem value="en">English</SelectItem>
                            <SelectItem value="zh">Chinese</SelectItem>
                          </SelectContent>
                        </Select>
                      </div>
                      <div className="overflow-x-auto">
                        <Table>
                          <TableHeader>
                            <TableRow>
                              <TableHead className="text-xs">Color</TableHead>
                              <TableHead className="text-xs">Code</TableHead>
                              <TableHead className="text-xs">Name</TableHead>
                            </TableRow>
                          </TableHeader>
                          <TableBody>
                            {groupBySemester(visibleCourses, (c) => c.semester).map((group) => (
                              <Fragment key={group.semester || "unknown"}>
                                <SemesterHeadingRow
                                  semester={group.semester}
                                  colSpan={3}
                                  current={group.semester === coursesData?.semester}
                                  count={`${group.items.length} course${group.items.length === 1 ? "" : "s"}`}
                                />
                              {[...group.items].sort((a, b) => (a.course_no ?? "").localeCompare(b.course_no ?? "")).map((c) => {
                                const paletteLight = coursesData?.palette_light ?? [];
                                const paletteDark = coursesData?.palette_dark ?? [];
                                const overrideIdx = c.color_hex ? paletteLight.findIndex((p: string) => p.toLowerCase() === c.color_hex!.toLowerCase()) : -1;
                                const isPresetOverride = overrideIdx >= 0;
                                const isCustom = !!c.color_hex && !isPresetOverride;
                                return (
                                  <TableRow key={c.id}>
                                    <TableCell>
                                      <div className="flex items-center gap-1">
                                        {isPresetOverride ? (
                                          <>
                                            <div className="h-4 w-4 rounded border" style={{ backgroundColor: paletteLight[overrideIdx] }} title="Light" />
                                            <div className="h-4 w-4 rounded border" style={{ backgroundColor: paletteDark[overrideIdx] }} title="Dark" />
                                            <span className="font-mono text-xs">#{overrideIdx}</span>
                                          </>
                                        ) : isCustom ? (
                                          <>
                                            <div className="h-4 w-4 rounded border" style={{ backgroundColor: c.color_hex! }} title="Custom" />
                                            <span className="font-mono text-xs">{c.color_hex}</span>
                                          </>
                                        ) : (
                                          <>
                                            <div className="h-4 w-4 rounded border" style={{ backgroundColor: c.default_color_light }} title="Light" />
                                            <div className="h-4 w-4 rounded border" style={{ backgroundColor: c.default_color_dark }} title="Dark" />
                                            <span className="font-mono text-xs text-muted-foreground">#{c.default_palette_index}</span>
                                          </>
                                        )}
                                      </div>
                                    </TableCell>
                                    <TableCell className="font-mono text-xs">{c.client_course_no}</TableCell>
                                    <TableCell className="text-xs max-w-[200px] truncate">
                                      {courseNameLang === "en" ? (c.course_name_en || c.course_name) : c.course_name}
                                    </TableCell>
                                  </TableRow>
                                );
                              })}
                              </Fragment>
                            ))}
                          </TableBody>
                        </Table>
                      </div>
                    </div>
                  )}
                </TabsContent>

                <TabsContent value="holiday-overrides">
                  {(() => {
                    const overrides = coursesData.holiday_overrides ?? [];
                    return overrides.length === 0 ? (
                      <p className="text-sm text-muted-foreground py-4">
                        No holiday exceptions synced. A device with cloud sync
                        off keeps this choice locally and never uploads it.
                      </p>
                    ) : (
                      <div className="overflow-x-auto">
                        <Table>
                          <TableHeader>
                            <TableRow>
                              <TableHead className="text-xs">Holiday</TableHead>
                              <TableHead className="text-xs">Dates</TableHead>
                              <TableHead className="text-xs">Class reminders</TableHead>
                              <TableHead className="text-xs">Updated</TableHead>
                            </TableRow>
                          </TableHeader>
                          <TableBody>
                            {overrides.map((o) => (
                              <TableRow key={o.holiday_id}>
                                <TableCell className="text-xs">
                                  <div>{o.name_zh}</div>
                                  <div className="text-muted-foreground">{o.name_en}</div>
                                </TableCell>
                                <TableCell className="text-xs whitespace-nowrap">
                                  {o.start_date === o.end_date
                                    ? o.start_date
                                    : `${o.start_date} \u2192 ${o.end_date}`}
                                </TableCell>
                                <TableCell className="text-xs">
                                  {o.notify ? "On (user opted in)" : "Off (default)"}
                                </TableCell>
                                <TableCell className="text-xs text-muted-foreground whitespace-nowrap">
                                  {o.updated_at ?? "\u2014"}
                                </TableCell>
                              </TableRow>
                            ))}
                          </TableBody>
                        </Table>
                      </div>
                    );
                  })()}
                </TabsContent>

                <TabsContent value="custom-names">
                  {(() => {
                    const withNames = visibleCourses
                      .map((c) => {
                        const names = typeof c.custom_names === "string" ? JSON.parse(c.custom_names || "{}") : c.custom_names;
                        return { ...c, parsedNames: (names && typeof names === "object") ? names as Record<string, string> : {} };
                      })
                      .filter((c) => Object.keys(c.parsedNames).length > 0)
                      .sort((a, b) => (a.course_no ?? "").localeCompare(b.course_no ?? ""));
                    return withNames.length === 0 ? (
                      <p className="text-sm text-muted-foreground py-4">
                        {activeSemester
                          ? `All ${activeSemester} courses using default names`
                          : "All courses using default names"}
                      </p>
                    ) : (
                      <div className="overflow-x-auto">
                        <Table>
                          <TableHeader>
                            <TableRow>
                              <TableHead className="text-xs">Course Code</TableHead>
                              <TableHead className="text-xs">Custom Name (Chinese)</TableHead>
                              <TableHead className="text-xs">Custom Name (English)</TableHead>
                            </TableRow>
                          </TableHeader>
                          <TableBody>
                            {groupBySemester(withNames, (c) => c.semester).map((group) => (
                              <Fragment key={group.semester || "unknown"}>
                                <SemesterHeadingRow
                                  semester={group.semester}
                                  colSpan={3}
                                  current={group.semester === coursesData?.semester}
                                  count={`${group.items.length} renamed`}
                                />
                              {group.items.map((c) => (
                                <TableRow key={c.id}>
                                  <TableCell className="font-mono text-xs">{c.client_course_no ?? c.course_no}</TableCell>
                                  <TableCell className="text-xs">{c.parsedNames["zh"] ?? <span className="text-muted-foreground">—</span>}</TableCell>
                                  <TableCell className="text-xs">{c.parsedNames["en"] ?? <span className="text-muted-foreground">—</span>}</TableCell>
                                </TableRow>
                              ))}
                              </Fragment>
                            ))}
                          </TableBody>
                        </Table>
                      </div>
                    );
                  })()}
                </TabsContent>

                <TabsContent value="assignments">
                  {visibleAssignments.length === 0 ? (
                    <p className="text-sm text-muted-foreground py-4">
                      {activeSemester && (coursesData?.assignments ?? []).length > 0
                        ? `No assignments in ${activeSemester} — ${(coursesData?.assignments ?? []).length} in other semesters.`
                        : "No assignments"}
                    </p>
                  ) : (
                    <div className="overflow-x-auto">
                      <Table>
                        <TableHeader>
                          <TableRow>
                            <TableHead className="text-xs">Title</TableHead>
                            <TableHead className="text-xs">Course</TableHead>
                            <TableHead className="text-xs">Due</TableHead>
                            <TableHead className="text-xs">Status</TableHead>
                          </TableRow>
                        </TableHeader>
                        <TableBody>
                          {groupBySemester(visibleAssignments, (a) => a.semester).map((group) => (
                            <Fragment key={group.semester || "unknown"}>
                              <SemesterHeadingRow
                                semester={group.semester}
                                colSpan={4}
                                current={group.semester === coursesData?.semester}
                                count={`${group.items.length} assignment${group.items.length === 1 ? "" : "s"}`}
                              />
                            {group.items.map((a) => {
                              const override = allOverrides.find((o) => o.moodle_assignment_id === a.moodle_assignment_id);
                              return (
                              <TableRow key={a.id}>
                                <TableCell className="text-xs max-w-[200px] truncate">{a.title}</TableCell>
                                <TableCell className="font-mono text-xs">{a.client_course_no || a.course_no || "—"}</TableCell>
                                <TableCell className="text-xs">{fmt(a.due_at)}</TableCell>
                                <TableCell>{override ? <RunStatusBadge status={override.local_status} /> : <span className="text-xs text-muted-foreground">normal</span>}</TableCell>
                              </TableRow>
                              );
                            })}
                            </Fragment>
                          ))}
                        </TableBody>
                      </Table>
                    </div>
                  )}
                </TabsContent>

                <TabsContent value="skipped-dates">
                  {(() => {
                    const rows = coursesData.course_skipped_dates ?? [];
                    return rows.length === 0 ? (
                      <p className="text-sm text-muted-foreground py-4">No skipped class dates synced.</p>
                    ) : (
                      <div className="overflow-x-auto">
                        <Table>
                          <TableHeader>
                            <TableRow>
                              <TableHead className="text-xs">Date</TableHead>
                              <TableHead className="text-xs">Course</TableHead>
                              <TableHead className="text-xs">Reason</TableHead>
                              <TableHead className="text-xs">Set by</TableHead>
                            </TableRow>
                          </TableHeader>
                          <TableBody>
                            {rows.map((r) => (
                              <TableRow key={r.id}>
                                <TableCell className="text-xs whitespace-nowrap">{r.skipped_on}</TableCell>
                                <TableCell className="text-xs">
                                  <span className="font-mono">{r.course_no || "—"}</span>
                                  {r.semester && <span className="text-muted-foreground"> · {r.semester}</span>}
                                </TableCell>
                                <TableCell className="text-xs">{r.reason ?? "—"}</TableCell>
                                <TableCell className="text-xs text-muted-foreground">{deviceLabel(r.created_by_device_id, data.devices ?? [])}</TableCell>
                              </TableRow>
                            ))}
                          </TableBody>
                        </Table>
                      </div>
                    );
                  })()}
                </TabsContent>

                <TabsContent value="settings">
                  {(() => {
                    const docs = coursesData.settings_documents ?? [];
                    return docs.length === 0 ? (
                      <p className="text-sm text-muted-foreground py-4">No settings documents synced.</p>
                    ) : (
                      <div className="space-y-3">
                        {docs.map((doc) => (
                          <div key={doc.namespace} className="rounded-md border p-3 space-y-2">
                            <div className="flex items-center gap-2 flex-wrap">
                              <Badge variant="outline" className="font-mono text-[10px]">{doc.namespace}</Badge>
                              <span className="text-xs text-muted-foreground ml-auto">
                                rev {doc.revision} &middot; schema v{doc.schema_version} &middot; {fmt(doc.updated_at)} &middot; by {deviceLabel(doc.updated_by_device_id, data.devices ?? [])}
                              </span>
                            </div>
                            <pre className="rounded bg-muted/40 p-2 text-[11px] leading-4 overflow-x-auto">{JSON.stringify(doc.document, null, 2)}</pre>
                          </div>
                        ))}
                      </div>
                    );
                  })()}
                </TabsContent>

                <TabsContent value="bulletins">
                  {(() => {
                    // Rules belong to one device and never sync, so only this
                    // device's are shown here.
                    const subs = (coursesData.bulletin_subscriptions ?? []).filter((s) => s.device_id === device.id);
                    const counts = coursesData.bulletin_state_counts;
                    const marked = coursesData.bulletin_states ?? [];
                    return (
                      <div className="space-y-4">
                        {subs.length === 0 ? (
                          <p className="text-sm text-muted-foreground py-2">No bulletin subscriptions on this device.</p>
                        ) : (
                          <div className="overflow-x-auto">
                            <Table>
                              <TableHeader>
                                <TableRow>
                                  <TableHead className="text-xs">Subscription</TableHead>
                                  <TableHead className="text-xs">Orgs</TableHead>
                                  <TableHead className="text-xs">Tags</TableHead>
                                  <TableHead className="text-xs">Mode</TableHead>
                                  <TableHead className="text-xs">Enabled</TableHead>
                                  <TableHead className="text-xs">Updated</TableHead>
                                </TableRow>
                              </TableHeader>
                              <TableBody>
                                {subs.map((s) => (
                                  <TableRow key={s.id}>
                                    <TableCell className="text-xs">{s.name || <span className="text-muted-foreground">#{s.id}</span>}</TableCell>
                                    <TableCell className="text-xs">{s.orgs.length > 0 ? s.orgs.join(", ") : "—"}</TableCell>
                                    <TableCell className="text-xs">{s.tags.length > 0 ? s.tags.join(", ") : "—"}</TableCell>
                                    <TableCell className="font-mono text-xs">{s.mode}</TableCell>
                                    <TableCell className="text-xs">{s.enabled ? "On" : "Off"}</TableCell>
                                    <TableCell className="text-xs text-muted-foreground whitespace-nowrap">{fmt(s.updated_at)}</TableCell>
                                  </TableRow>
                                ))}
                              </TableBody>
                            </Table>
                          </div>
                        )}
                        <p className="text-xs text-muted-foreground">
                          Bulletin state: {counts?.read ?? 0} read &middot; {counts?.starred ?? 0} starred &middot; {counts?.hidden ?? 0} hidden
                        </p>
                        {marked.length > 0 && (
                          <div className="overflow-x-auto">
                            <Table>
                              <TableHeader>
                                <TableRow>
                                  <TableHead className="text-xs">Bulletin</TableHead>
                                  <TableHead className="text-xs">State</TableHead>
                                  <TableHead className="text-xs">Updated</TableHead>
                                </TableRow>
                              </TableHeader>
                              <TableBody>
                                {marked.map((b) => (
                                  <TableRow key={b.bulletin_id}>
                                    <TableCell className="text-xs max-w-[260px] truncate" title={b.title}>{b.title}</TableCell>
                                    <TableCell className="text-xs">
                                      {[b.is_starred && "starred", b.is_hidden && "hidden", b.is_read && "read"].filter(Boolean).join(", ")}
                                    </TableCell>
                                    <TableCell className="text-xs text-muted-foreground whitespace-nowrap">{fmt(b.updated_at)}</TableCell>
                                  </TableRow>
                                ))}
                              </TableBody>
                            </Table>
                          </div>
                        )}
                      </div>
                    );
                  })()}
                </TabsContent>

              </Tabs>
            )}
          </TabsContent>
          <TabsContent value="synced-list">
            <SyncedList device={device} coursesData={coursesData} />
          </TabsContent>
          <TabsContent value="push">
            <div className="mb-4 flex flex-wrap gap-2">
              <Button
                size="sm"
                variant="outline"
                onClick={async () => {
                  // Every branch has to name a reason. The previous version
                  // read only `resp.error`, so a FastAPI rejection — which
                  // comes back as `detail` — surfaced as "unknown", and the
                  // catch discarded the exception entirely. Both produced a
                  // failure with nothing to act on.
                  try {
                    const res = await fetch("/api/moodle/push-tick", { method: "POST" });
                    const body = await res.text();
                    let resp: { ok?: boolean; error?: string; detail?: string } = {};
                    try {
                      resp = JSON.parse(body);
                    } catch {
                      resp = { error: body.slice(0, 300) || `HTTP ${res.status}` };
                    }
                    if (!res.ok || resp.ok === false) {
                      alert(
                        "Push tick failed: " +
                          (resp.error ?? resp.detail ?? `HTTP ${res.status}`)
                      );
                    }
                  } catch (e) {
                    alert("Push tick request failed: " + (e instanceof Error ? e.message : String(e)));
                  }
                }}
              >
                Force Execute Pipeline
              </Button>
              {(data.push_jobs ?? []).length > 0 && (
                <Button
                  size="sm"
                  variant="outline"
                  onClick={async () => {
                    if (!confirm(`Cancel all ${(data.push_jobs ?? []).length} pending push jobs?`)) return;
                    try {
                      const res = await fetch(`/api/moodle/push-clear?student_id=${encodeURIComponent(studentId)}`, { method: "POST" });
                      const resp = await res.json();
                      if (!resp.ok) alert("Clear failed: " + (resp.error ?? "unknown"));
                    } catch {
                      alert("Clear request failed");
                    }
                  }}
                >
                  Clear Queue
                </Button>
              )}
              <Button
                size="sm"
                variant="default"
                onClick={async () => {
                  try {
                    const res = await fetch(`/api/moodle/push-sync-trigger?student_id=${encodeURIComponent(studentId)}`, { method: "POST" });
                    const resp = await res.json();
                    if (resp.deduplicated) alert("Deduplicated — a sync_trigger already exists in this window");
                    else if (!resp.ok) alert("Failed: " + (resp.error ?? "unknown"));
                  } catch {
                    alert("Request failed");
                  }
                }}
              >
                Force Sync Push
              </Button>
            </div>
            {deviceDeliveries.length === 0 ? (
              <div className="py-4 text-sm text-muted-foreground">No push deliveries for this device.</div>
            ) : (
              <div className="overflow-x-auto">
                <Table>
                  <TableHeader>
                    <TableRow>
                      <TableHead className="text-xs">Job ID</TableHead>
                      <TableHead className="text-xs">Provider</TableHead>
                      <TableHead className="text-xs">Status</TableHead>
                      <TableHead className="text-xs">Attempts</TableHead>
                      <TableHead className="text-xs">Sent</TableHead>
                      <TableHead className="text-xs">Error</TableHead>
                    </TableRow>
                  </TableHeader>
                  <TableBody>
                    {deviceDeliveries.map((dl) => (
                      <TableRow key={dl.id}>
                        <TableCell className="font-mono text-xs">#{dl.push_job_id}</TableCell>
                        <TableCell><Badge variant="outline" className="text-[10px]">{dl.provider}</Badge></TableCell>
                        <TableCell>
                          <Badge
                            variant={dl.status === "sent" ? "default" : dl.status === "failed" ? "destructive" : "secondary"}
                            className="text-[10px]"
                          >
                            {dl.status}
                          </Badge>
                        </TableCell>
                        <TableCell className="text-xs">{dl.attempts}/{dl.max_attempts}</TableCell>
                        <TableCell className="text-xs">{fmt(dl.sent_at)}</TableCell>
                        <TableCell className="text-xs text-destructive">{dl.failure_code ?? "—"}</TableCell>
                      </TableRow>
                    ))}
                  </TableBody>
                </Table>
              </div>
            )}
          </TabsContent>
          <TabsContent value="queued">
            <QueuedJobs jobs={deviceQueue} deviceId={device.id} />
          </TabsContent>
          <TabsContent value="source-push">
            {devicePushJobs.length === 0 ? (
              <div className="py-4 text-sm text-muted-foreground">No push jobs sourced from this device.</div>
            ) : (
              <div className="space-y-4">
                {devicePushJobs.map((pj) => {
                  const deliveries = (data.push_deliveries ?? []).filter((d) => d.push_job_id === pj.id);
                  return (
                    <div key={pj.id} className="border rounded-md p-3 space-y-2">
                      <div className="flex items-center gap-2 flex-wrap">
                        <Badge variant={pj.status === "pending" ? "default" : "secondary"}>{pj.status}</Badge>
                        <span className="font-mono text-xs">{pj.scenario}</span>
                        <span className="text-xs text-muted-foreground ml-auto">
                          #{pj.id} &middot; {pj.attempts}/{pj.max_attempts} attempts &middot; fires {fmt(pj.fire_at)}
                        </span>
                      </div>
                      {pj.last_error && <p className="text-xs text-destructive">{pj.last_error}</p>}
                      {deliveries.length > 0 && (
                        <div className="overflow-x-auto">
                          <Table>
                            <TableHeader>
                              <TableRow>
                                <TableHead className="text-xs">Target</TableHead>
                                <TableHead className="text-xs">Provider</TableHead>
                                <TableHead className="text-xs">Status</TableHead>
                                <TableHead className="text-xs">Attempts</TableHead>
                                <TableHead className="text-xs">Error</TableHead>
                              </TableRow>
                            </TableHeader>
                            <TableBody>
                              {deliveries.map((dl) => {
                                const target = (data.devices ?? []).find((d) => d.id === dl.device_id);
                                return (
                                  <TableRow key={dl.id}>
                                    <TableCell className="text-xs">
                                      {target ? (
                                        <><Badge variant="outline" className="text-[10px] px-1 py-0">{target.platform}</Badge> {target.client_device_id.slice(0, 8)}...</>
                                      ) : (
                                        <span className="text-muted-foreground">{dl.device_id?.slice(0, 8) ?? "—"}...</span>
                                      )}
                                    </TableCell>
                                    <TableCell><Badge variant="outline" className="text-[10px]">{dl.provider}</Badge></TableCell>
                                    <TableCell>
                                      <Badge variant={dl.status === "sent" ? "default" : dl.status === "failed" ? "destructive" : "secondary"} className="text-[10px]">{dl.status}</Badge>
                                    </TableCell>
                                    <TableCell className="text-xs">{dl.attempts}/{dl.max_attempts}</TableCell>
                                    <TableCell className="text-xs text-destructive">{dl.failure_code ?? "—"}</TableCell>
                                  </TableRow>
                                );
                              })}
                            </TableBody>
                          </Table>
                        </div>
                      )}
                    </div>
                  );
                })}
              </div>
            )}
          </TabsContent>
          <TabsContent value="info">
            <div className="overflow-x-auto">
              <Table>
                <TableBody>
                  <TableRow>
                    <TableCell className="font-medium text-xs w-36">Device ID</TableCell>
                    <TableCell className="font-mono text-xs break-all">{device.client_device_id}</TableCell>
                  </TableRow>
                  <TableRow>
                    <TableCell className="font-medium text-xs">Internal ID</TableCell>
                    <TableCell className="font-mono text-xs">{device.id}</TableCell>
                  </TableRow>
                  <TableRow>
                    <TableCell className="font-medium text-xs">Platform</TableCell>
                    <TableCell className="text-xs">{platformLabel(device.platform)}</TableCell>
                  </TableRow>
                  <TableRow>
                    <TableCell className="font-medium text-xs">Device Class</TableCell>
                    <TableCell className="text-xs">{device.device_class || "—"}</TableCell>
                  </TableRow>
                  <TableRow>
                    <TableCell className="font-medium text-xs">Model</TableCell>
                    <TableCell className="text-xs">{device.device_model ?? device.device_name ?? "—"}</TableCell>
                  </TableRow>
                  <TableRow>
                    <TableCell className="font-medium text-xs">Locale</TableCell>
                    <TableCell className="text-xs">{device.locale ?? "—"}</TableCell>
                  </TableRow>
                  <TableRow>
                    <TableCell className="font-medium text-xs">App Version</TableCell>
                    <TableCell className="text-xs">{device.app_version ?? "—"}</TableCell>
                  </TableRow>
                  <TableRow>
                    <TableCell className="font-medium text-xs">OS Version</TableCell>
                    <TableCell className="text-xs">{device.os_version ? `${platformLabel(device.platform)} ${device.os_version}` : "—"}</TableCell>
                  </TableRow>
                  <TableRow>
                    <TableCell className="font-medium text-xs">Last Seen</TableCell>
                    <TableCell className="text-xs">{fmt(device.last_seen_at)}</TableCell>
                  </TableRow>
                  <TableRow>
                    <TableCell className="font-medium text-xs">Last Login</TableCell>
                    <TableCell className="text-xs">{fmt(device.last_login_at)}</TableCell>
                  </TableRow>
                  <TableRow>
                    <TableCell className="font-medium text-xs">Registered</TableCell>
                    <TableCell className="text-xs">{fmt(device.created_at)}</TableCell>
                  </TableRow>
                </TableBody>
              </Table>
            </div>
          </TabsContent>
        </Tabs>
      </CardContent>
    </Card>
  );
}

// ── Queued Jobs ─────────────────────────────────────────────────────────

/** What the job is, in the words the apps use. */
function queuedJobLabel(j: QueuedJob): string {
  const offset = /^reminder_(\d+(?:\.\d+)?)(m|h)$/.exec(j.scenario);
  const lead = offset ? ` · ${offset[1]} ${offset[2] === "m" ? "min" : "h"} before` : "";
  switch (j.channel) {
    case "schedule": {
      if (j.kind === "live_activity_end") return "Live Activity end";
      const scenarios: Record<string, string> = {
        classPreparing: "class preparing",
        inClass: "in class",
        assignmentUrgent: "assignment due",
      };
      return `Live Activity start · ${scenarios[j.scenario] ?? j.scenario}`;
    }
    case "course":
      return `Class reminder${lead}`;
    case "assignment":
      return `Assignment due reminder${lead}`;
    case "bulletin":
      return "Bulletin";
    case "system":
      if (j.scenario === "sync_trigger") return "Silent sync trigger";
      if (j.scenario === "reauth_required") return "Sign-in expired notice";
      return `System · ${j.scenario}`;
    case "custom":
      return "Custom push";
    default:
      return `${j.channel} · ${j.scenario}`;
  }
}

/** The token a job's push needs, for the "will not arrive" note. */
function queuedJobToken(j: QueuedJob): string {
  if (j.channel !== "schedule") return "push";
  return j.kind === "live_activity_end" ? "Live Activity update" : "push-to-start";
}

/**
 * Everything the server has queued that it will push to this device,
 * soonest first. The backend resolves recipients with the push pipeline's
 * own rules, so a job is listed only if this device would receive it. A
 * device missing the token the push needs still lists the job, flagged:
 * it is planned for the device but will not arrive.
 */
function QueuedJobs({ jobs, deviceId }: { jobs: QueuedJob[]; deviceId: string }) {
  if (jobs.length === 0) {
    return <div className="py-4 text-sm text-muted-foreground">Nothing queued for this device.</div>;
  }
  return (
    <div className="space-y-2 py-2">
      <p className="text-xs text-muted-foreground">
        Pending jobs addressed to this device, soonest first. Only jobs the server has already created appear — Live Activity starts come from the device's own 48-hour upload and class reminders are created 48 hours ahead — so later ones show up as their time approaches.
      </p>
      <div className="overflow-x-auto">
        <Table>
          <TableHeader>
            <TableRow>
              <TableHead className="text-xs">When</TableHead>
              <TableHead className="text-xs">What</TableHead>
              <TableHead className="text-xs">Content</TableHead>
              <TableHead className="text-xs">Status</TableHead>
            </TableRow>
          </TableHeader>
          <TableBody>
            {jobs.map((j) => {
              const ready = j.recipients.find((r) => r.device_id === deviceId)?.token_ready ?? false;
              return (
                <TableRow key={j.id} className={ready ? "" : "opacity-60"}>
                  <TableCell className="text-xs whitespace-nowrap">
                    <div>{timeUntil(j.fire_at)}</div>
                    <div className="text-muted-foreground">{fmt(j.fire_at)}</div>
                  </TableCell>
                  <TableCell className="text-xs">
                    <div className="font-medium">{queuedJobLabel(j)}</div>
                    <div className="font-mono text-[10px] text-muted-foreground">#{j.id}</div>
                  </TableCell>
                  <TableCell className="text-xs max-w-[260px]">
                    <div className="truncate" title={j.title}>{j.title || "—"}</div>
                    {j.body && <div className="truncate text-muted-foreground" title={j.body}>{j.body}</div>}
                  </TableCell>
                  <TableCell className="text-xs">
                    <Badge variant={j.status === "processing" ? "default" : "secondary"} className="text-[10px]">{j.status}</Badge>
                    {j.attempts > 0 && <div className="text-muted-foreground mt-1">{j.attempts}/{j.max_attempts} attempts</div>}
                    {!ready && <div className="text-orange-500 mt-1">No {queuedJobToken(j)} token · will not arrive</div>}
                    {j.last_error && <div className="text-destructive mt-1">{j.last_error}</div>}
                  </TableCell>
                </TableRow>
              );
            })}
          </TableBody>
        </Table>
      </div>
    </div>
  );
}

// ── Synced List ─────────────────────────────────────────────────────────

type SyncedRow = {
  label: string;
  /** This device's own flag for the row. */
  enabled: boolean;
  /** The account holds data for it, whether or not this device syncs it. */
  hasData: boolean;
  detail: string;
  /** A 同步內容 row, which only applies while the master is on. */
  child?: boolean;
};

/**
 * What this device takes part in, row for row with the apps' TigerSync
 * screen (spec §4.6's toggle-to-column table): 同步課程資訊, the master
 * every 同步內容 row sits under, and the two push channels, which are
 * independent of it. Bulletin subscriptions are not here: they belong to
 * one device and are never synced, and Data › Bulletins lists them. A row switched off
 * here still says what the account holds, because other devices keep
 * syncing it.
 */
function SyncedList({ device, coursesData }: { device: SyncDevice; coursesData?: SyncCoursesResponse }) {
  const courses = coursesData?.courses ?? [];
  const assignments = coursesData?.assignments ?? [];
  const notification = (coursesData?.settings_documents ?? [])
    .find((d) => d.namespace === "notification")?.document;
  const colorCount = courses.filter((c) => c.color_hex).length;
  const customNameCount = courses.filter((c) => Object.keys(parseNames(c.custom_names)).length > 0).length;
  const master = device.cloud_sync_enabled !== false;

  const groups: { title: string; rows: SyncedRow[] }[] = [
    {
      title: "同步課程資訊 (sync course information)",
      rows: [
        {
          label: "同步課程資訊",
          enabled: master,
          hasData: courses.length > 0 || assignments.length > 0,
          detail: master ? "On — the rows below apply" : "Off — this device keeps its course data locally",
        },
        {
          label: "Assignment status",
          enabled: device.sync_assignments !== false,
          hasData: assignments.length > 0,
          detail: `${assignments.length} assignments in backend`,
          child: true,
        },
        {
          label: "Assignment due reminders",
          enabled: device.sync_assignment_reminders !== false,
          hasData: !!asRecord(notification?.assignments),
          detail: reminderDetail(asRecord(notification?.assignments)),
          child: true,
        },
        {
          label: "Live Activity / Live Updates",
          enabled: device.sync_live_activity !== false,
          hasData: !!asRecord(notification?.live_activity),
          detail: liveActivityDetail(asRecord(notification?.live_activity)),
          child: true,
        },
        {
          label: "Class table – all courses",
          enabled: device.sync_courses !== false,
          hasData: courses.length > 0,
          detail: `${courses.length} courses in backend`,
          child: true,
        },
        {
          label: "Course colours",
          enabled: device.sync_course_colors !== false,
          hasData: colorCount > 0,
          detail: colorCount > 0 ? `${colorCount} of ${courses.length} have synced colours` : "All using auto-assigned colours",
          child: true,
        },
        {
          label: "Custom course names",
          enabled: device.sync_course_names !== false,
          hasData: customNameCount > 0,
          detail: customNameCount > 0 ? `${customNameCount} of ${courses.length} have custom names` : "All using default names",
          child: true,
        },
      ],
    },
    {
      title: "Push",
      rows: [
        {
          label: "接收額外伺服器推播 (extra server pushes)",
          enabled: device.server_push_enabled !== false,
          hasData: false,
          detail: "Operator custom pushes",
        },
        {
          label: "Bulletin pushes",
          enabled: device.bulletin_push_enabled !== false,
          hasData: false,
          detail: "New bulletins matching a subscription",
        },
      ],
    },
  ];

  return (
    <div className="space-y-4 py-2">
      {groups.map((g) => (
        <div key={g.title} className="space-y-2">
          <div className="text-xs font-medium text-muted-foreground">{g.title}</div>
          {g.rows.map((row) => (
            <SyncedRowView key={row.label} row={row} masterOff={!!row.child && !master} />
          ))}
        </div>
      ))}
    </div>
  );
}

function SyncedRowView({ row, masterOff }: { row: SyncedRow; masterOff: boolean }) {
  const dotColor = masterOff
    ? "bg-muted-foreground/30"
    : row.enabled ? "bg-green-500" : row.hasData ? "bg-orange-400" : "bg-muted-foreground/30";
  const note = masterOff ? " · 同步課程資訊 off" : !row.enabled ? " · off on this device" : "";
  return (
    <div className={`flex items-center gap-3 rounded-md border border-border p-3 ${row.child ? "ml-5" : ""} ${masterOff ? "opacity-60" : ""}`}>
      <div className={`h-2.5 w-2.5 rounded-full shrink-0 ${dotColor}`} />
      <div className="min-w-0">
        <div className="text-sm font-medium">{row.label}</div>
        <div className="text-xs text-muted-foreground">{row.detail}{note}</div>
      </div>
    </div>
  );
}

function parseNames(v: unknown): Record<string, string> {
  const parsed = typeof v === "string" ? JSON.parse(v || "{}") : v;
  return parsed && typeof parsed === "object" ? (parsed as Record<string, string>) : {};
}

function asRecord(v: unknown): Record<string, unknown> | undefined {
  return v && typeof v === "object" && !Array.isArray(v) ? (v as Record<string, unknown>) : undefined;
}

function numberList(v: unknown): number[] {
  return Array.isArray(v) ? v.filter((n): n is number => typeof n === "number") : [];
}

/**
 * "24h, 30m before", read the way the reminder scanner reads it
 * (`server/push/reminders.py::_offsets_hours`): a minutes list is the whole
 * answer whenever it is present, empty included; the hours list, all a
 * pre-2.1.0 app writes, only counts when it is not.
 */
function formatOffsets(section: Record<string, unknown>): string {
  const values = Array.isArray(section.reminder_offsets_minutes)
    ? numberList(section.reminder_offsets_minutes)
    : Array.isArray(section.reminder_offsets_hours)
      ? numberList(section.reminder_offsets_hours).map((h) => h * 60)
      : null;
  if (values === null) return "default offsets";
  if (values.length === 0) return "every offset off";
  return [...values]
    .sort((a, b) => b - a)
    .map((m) => (m % 60 === 0 ? `${m / 60}h` : `${m}m`))
    .join(", ") + " before";
}

/** A missing `enabled` counts as on, as it does for the scanner. */
function reminderDetail(section: Record<string, unknown> | undefined): string {
  if (!section) return "No account setting synced yet";
  if (section.enabled === false) return "Account setting: off";
  return `Account setting: on · ${formatOffsets(section)}`;
}

function liveActivityDetail(section: Record<string, unknown> | undefined): string {
  if (!section) return "No account setting synced yet";
  const shown = [
    section.show_class_preparing !== false && "class preparing",
    section.show_in_class !== false && "in class",
    section.show_assignment !== false && "assignments",
  ].filter(Boolean);
  return shown.length > 0 ? `Account setting: shows ${shown.join(", ")}` : "Account setting: every kind hidden";
}
