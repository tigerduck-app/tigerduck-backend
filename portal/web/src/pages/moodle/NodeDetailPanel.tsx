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
import { RunStatusBadge, deviceLabel, fmt, platformIcon, platformLabel, relativeTime } from "./format";
import type { SyncCoursesResponse, SyncEventsResponse, TopologyNode } from "./types";

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

  const allCourses = coursesData?.courses ?? [];
  const allAssignments = coursesData?.assignments ?? [];
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
            <TabsTrigger value="source-push">
              Source Jobs{devicePushJobs.length > 0 ? ` (${devicePushJobs.length})` : ""}
            </TabsTrigger>
            <TabsTrigger value="info">Info</TabsTrigger>
          </TabsList>
          <TabsContent value="data">
            {device.cloud_sync_enabled === false ? (
              <div className="flex items-center gap-3 rounded-md border border-border p-4 my-2">
                <div className="h-2.5 w-2.5 rounded-full shrink-0 bg-muted-foreground/30" />
                <div className="min-w-0">
                  <div className="text-sm font-medium">Device only</div>
                  <div className="text-xs text-muted-foreground">Cloud Sync is off — data on this device is not tracked by the server</div>
                </div>
              </div>
            ) : !coursesData ? (
              <div className="flex items-center justify-center py-6 text-muted-foreground">
                <Loader2 className="mr-2 h-4 w-4 animate-spin" /> Loading...
              </div>
            ) : (
              <Tabs defaultValue="courses">
                <div className="mb-2 flex items-center justify-between gap-2">
                  <TabsList>
                    <TabsTrigger value="courses">Courses ({visibleCourses.length})</TabsTrigger>
                    <TabsTrigger value="custom-names">Custom Names ({visibleCourses.filter((c) => { const n = typeof c.custom_names === "string" ? JSON.parse(c.custom_names || "{}") : c.custom_names; return n && typeof n === "object" && Object.keys(n).length > 0; }).length})</TabsTrigger>
                    <TabsTrigger value="assignments">Assignments ({visibleAssignments.length})</TabsTrigger>
                    <TabsTrigger value="holiday-overrides">Holiday Overrides ({(coursesData.holiday_overrides ?? []).length})</TabsTrigger>
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

              </Tabs>
            )}
          </TabsContent>
          <TabsContent value="synced-list">
            <div className="space-y-2 py-2">
              {device.cloud_sync_enabled === false ? (
                <div className="flex items-center gap-3 rounded-md border border-border p-3">
                  <div className="h-2.5 w-2.5 rounded-full shrink-0 bg-muted-foreground/30" />
                  <div className="min-w-0">
                    <div className="text-sm font-medium">Device local only</div>
                    <div className="text-xs text-muted-foreground">Cloud Sync is off — this device is not participating in cross-device sync</div>
                  </div>
                </div>
              ) : (() => {
                const colorCount = allCourses.filter((c) => c.color_hex).length;
                const customNameCount = allCourses.filter((c) => {
                  const names = typeof c.custom_names === "string" ? JSON.parse(c.custom_names || "{}") : c.custom_names;
                  return names && typeof names === "object" && Object.keys(names).length > 0;
                }).length;
                const categories = [
                  { label: "Courses", count: allCourses.length, enabled: device.sync_courses !== false, detail: `${allCourses.length} courses in backend` },
                  { label: "Course colours", count: colorCount, enabled: device.sync_course_colors !== false, detail: colorCount > 0 ? `${colorCount} of ${allCourses.length} have synced colours` : `All using auto-assigned colours` },
                  { label: "Custom course names", count: customNameCount, enabled: device.sync_course_names !== false, detail: customNameCount > 0 ? `${customNameCount} of ${allCourses.length} have custom names` : `All using default names` },
                  { label: "Assignments", count: allAssignments.length, enabled: device.sync_assignments !== false, detail: `${allAssignments.length} assignments in backend` },
                ];
                return categories.map((cat) => {
                  const dotColor = cat.enabled
                    ? "bg-green-500"
                    : cat.count > 0 ? "bg-orange-400" : "bg-muted-foreground/30";
                  const statusNote = !cat.enabled && cat.count > 0 ? " · sync to other devices off" : "";
                  return (
                  <div key={cat.label} className="flex items-center gap-3 rounded-md border border-border p-3">
                    <div className={`h-2.5 w-2.5 rounded-full shrink-0 ${dotColor}`} />
                    <div className="min-w-0">
                      <div className="text-sm font-medium">{cat.label}</div>
                      <div className="text-xs text-muted-foreground">{cat.detail}{statusNote}</div>
                    </div>
                  </div>
                  );
                });
              })()}
            </div>
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
