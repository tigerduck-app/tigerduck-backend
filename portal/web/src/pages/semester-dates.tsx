// Semester dates and school holidays — the operator side of the feed the
// apps read from /v3/calendar/semesters.
//
// Both apps used to carry the current term as a hardcoded constant, so a
// new term meant a store release and a holiday could not be expressed at
// all. Everything on this page is that constant, made editable.

import { useMemo, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { toast } from "sonner";
import { AlertTriangle, CalendarDays, Pencil, Plus, Trash2 } from "lucide-react";
import { api, ApiError } from "@/lib/api";
import { Button } from "@/components/ui/button";
import { Card, CardContent } from "@/components/ui/card";
import {
  Dialog,
  DialogContent,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { PageHeader, Section } from "@/components/ui/section";
import { Skeleton } from "@/components/ui/skeleton";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";

type Term = {
  code: string;
  start_date: string;
  end_date: string;
  name_zh: string;
  name_en: string;
};

type Holiday = {
  id: number;
  name_zh: string;
  name_en: string;
  start_date: string;
  end_date: string;
};

/** A between-term stretch that no holiday covers — see `_uncovered_gaps`. */
type Gap = {
  after: string;
  before: string;
  start_date: string;
  end_date: string;
  uncovered_days: number;
};

type CalendarResponse = {
  terms: Term[];
  holidays: Holiday[];
  /** Terms the clients have filed courses under that have no dates yet. */
  undated_terms: string[];
  gaps: Gap[];
};

/** "1151" -> "115-1", matching how both apps render a term. */
function formatCode(code: string): string {
  return code.length === 4 ? `${code.slice(0, 3)}-${code.slice(3)}` : code;
}

function formatRange(start: string, end: string): string {
  return start === end ? start : `${start} → ${end}`;
}

const EMPTY_TERM: Term = {
  code: "",
  start_date: "",
  end_date: "",
  name_zh: "",
  name_en: "",
};

const EMPTY_HOLIDAY: Omit<Holiday, "id"> & { id: number | null } = {
  id: null,
  name_zh: "",
  name_en: "",
  start_date: "",
  end_date: "",
};

export function SemesterDatesPage() {
  const qc = useQueryClient();
  const [termDraft, setTermDraft] = useState<Term | null>(null);
  const [holidayDraft, setHolidayDraft] = useState<typeof EMPTY_HOLIDAY | null>(
    null,
  );

  const calendarQ = useQuery<CalendarResponse>({
    queryKey: ["academic-calendar"],
    queryFn: () => api<CalendarResponse>("/api/academic-calendar"),
  });

  const invalidate = () =>
    qc.invalidateQueries({ queryKey: ["academic-calendar"] });

  const onError = (e: unknown) =>
    toast.error(e instanceof ApiError ? e.message : "Request failed");

  const saveTerm = useMutation({
    mutationFn: (t: Term) =>
      api<Term>(`/api/academic-calendar/terms/${encodeURIComponent(t.code)}`, {
        method: "PUT",
        json: t,
      }),
    onSuccess: () => {
      setTermDraft(null);
      toast.success("Semester saved");
      invalidate();
    },
    onError,
  });

  const deleteTerm = useMutation({
    mutationFn: (code: string) =>
      api(`/api/academic-calendar/terms/${encodeURIComponent(code)}`, {
        method: "DELETE",
      }),
    onSuccess: () => {
      toast.success("Semester removed");
      invalidate();
    },
    onError,
  });

  const saveHoliday = useMutation({
    mutationFn: (h: typeof EMPTY_HOLIDAY) =>
      h.id === null
        ? api<Holiday>("/api/academic-calendar/holidays", {
            method: "POST",
            json: h,
          })
        : api<Holiday>(`/api/academic-calendar/holidays/${h.id}`, {
            method: "PUT",
            json: h,
          }),
    onSuccess: () => {
      setHolidayDraft(null);
      toast.success("Holiday saved");
      invalidate();
    },
    onError,
  });

  const deleteHoliday = useMutation({
    mutationFn: (id: number) =>
      api(`/api/academic-calendar/holidays/${id}`, { method: "DELETE" }),
    onSuccess: () => {
      toast.success("Holiday removed");
      invalidate();
    },
    onError,
  });

  const data = calendarQ.data;

  /**
   * Every term the operator might want dates for: the ones already dated,
   * plus codes the clients have actually uploaded courses under.
   *
   * Discovered codes are listed rather than typed because the code is the
   * school's own four-character form ("1151"), not the "115-1" the apps
   * display — and a term filed under a typo would silently never match a
   * course row. An undated entry has no `semester_terms` row at all, so the
   * apps never see it.
   */
  /** Codes the database already knows, so the dialog can stop the operator
   *  retyping (and mistyping) one. */
  const knownCodes = useMemo(
    () =>
      new Set([
        ...(data?.terms ?? []).map((t) => t.code),
        ...(data?.undated_terms ?? []),
      ]),
    [data],
  );

  const rows = useMemo(() => {
    if (!data) return [];
    const dated = data.terms.map((t) => ({ ...t, dated: true }));
    const known = new Set(data.terms.map((t) => t.code));
    const discovered = data.undated_terms
      .filter((code) => !known.has(code))
      .map((code) => ({ ...EMPTY_TERM, code, dated: false }));
    return [...dated, ...discovered].sort((a, b) => b.code.localeCompare(a.code));
  }, [data]);

  const warnings = useMemo(() => {
    if (!data) return [];
    const out: { key: string; text: string }[] = data.gaps.map((g) => ({
      key: `gap-${g.after}-${g.before}`,
      text:
        `${g.uncovered_days} day(s) between ${formatCode(g.after)} and ` +
        `${formatCode(g.before)} (${formatRange(g.start_date, g.end_date)}) ` +
        `have no holiday. Devices keep their last timetable, so class ` +
        `reminders will fire on those days.`,
    }));
    return out;
  }, [data]);

  return (
    <div className="space-y-6">
      <PageHeader
        title="Semester dates"
        description="Term ranges and school holidays. Served to every app — signed in or not — from /v3/calendar/semesters."
      />

      {warnings.length > 0 && (
        <Card className="border-amber-500/50 bg-amber-500/5">
          <CardContent className="space-y-2 py-4">
            {warnings.map((w) => (
              <div key={w.key} className="flex gap-2 text-sm">
                <AlertTriangle className="mt-0.5 h-4 w-4 shrink-0 text-amber-500" />
                <span>{w.text}</span>
              </div>
            ))}
          </CardContent>
        </Card>
      )}

      <Section
        title="Semesters"
        actions={
          <Button size="sm" onClick={() => setTermDraft({ ...EMPTY_TERM })}>
            <Plus className="mr-1 h-4 w-4" /> Add semester
          </Button>
        }
      >
        {calendarQ.isLoading ? (
          <Skeleton className="h-24 w-full" />
        ) : !data || rows.length === 0 ? (
          <p className="py-4 text-sm text-muted-foreground">
            No semesters yet. Until one covers today, the apps fall back to
            their month-based guess and suppress nothing.
          </p>
        ) : (
          <Table>
            <TableHeader>
              <TableRow>
                <TableHead>Term</TableHead>
                <TableHead>First day</TableHead>
                <TableHead>Last day</TableHead>
                <TableHead className="w-24" />
              </TableRow>
            </TableHeader>
            <TableBody>
              {rows.map((t) => (
                <TableRow key={t.code}>
                  <TableCell className="font-medium">
                    {formatCode(t.code)}
                    <span className="ml-2 text-xs text-muted-foreground">
                      {t.code}
                    </span>
                  </TableCell>
                  {t.dated ? (
                    <>
                      <TableCell>{t.start_date}</TableCell>
                      <TableCell>{t.end_date}</TableCell>
                    </>
                  ) : (
                    <TableCell colSpan={2} className="text-muted-foreground">
                      Not set — the apps ignore this term until it has dates
                    </TableCell>
                  )}
                  <TableCell className="text-right">
                    <Button
                      variant="ghost"
                      size="icon"
                      onClick={() =>
                        setTermDraft({
                          code: t.code,
                          start_date: t.start_date,
                          end_date: t.end_date,
                          name_zh: t.name_zh,
                          name_en: t.name_en,
                        })
                      }
                    >
                      <Pencil className="h-4 w-4" />
                    </Button>
                    {t.dated && (
                      <Button
                        variant="ghost"
                        size="icon"
                        onClick={() => deleteTerm.mutate(t.code)}
                      >
                        <Trash2 className="h-4 w-4" />
                      </Button>
                    )}
                  </TableCell>
                </TableRow>
              ))}
            </TableBody>
          </Table>
        )}
      </Section>

      <Section
        title="Holidays"
        actions={
          <Button
            size="sm"
            onClick={() => setHolidayDraft({ ...EMPTY_HOLIDAY })}
          >
            <Plus className="mr-1 h-4 w-4" /> Add holiday
          </Button>
        }
      >
        {calendarQ.isLoading ? (
          <Skeleton className="h-24 w-full" />
        ) : !data || data.holidays.length === 0 ? (
          <p className="py-4 text-sm text-muted-foreground">
            No holidays yet. Class reminders fire every scheduled day until one
            is added.
          </p>
        ) : (
          <Table>
            <TableHeader>
              <TableRow>
                <TableHead>Name</TableHead>
                <TableHead>Dates</TableHead>
                <TableHead className="w-24" />
              </TableRow>
            </TableHeader>
            <TableBody>
              {data.holidays.map((h) => (
                <TableRow key={h.id}>
                  <TableCell>
                    <div className="font-medium">{h.name_zh}</div>
                    <div className="text-xs text-muted-foreground">
                      {h.name_en}
                    </div>
                  </TableCell>
                  <TableCell className="whitespace-nowrap">
                    {formatRange(h.start_date, h.end_date)}
                  </TableCell>
                  <TableCell className="text-right">
                    <Button
                      variant="ghost"
                      size="icon"
                      onClick={() => setHolidayDraft({ ...h })}
                    >
                      <Pencil className="h-4 w-4" />
                    </Button>
                    <Button
                      variant="ghost"
                      size="icon"
                      onClick={() => deleteHoliday.mutate(h.id)}
                    >
                      <Trash2 className="h-4 w-4" />
                    </Button>
                  </TableCell>
                </TableRow>
              ))}
            </TableBody>
          </Table>
        )}
      </Section>

      <Dialog
        open={termDraft !== null}
        onOpenChange={(o) => !o && setTermDraft(null)}
      >
        <DialogContent>
          <DialogHeader>
            <DialogTitle>
              <CalendarDays className="mr-2 inline h-4 w-4" />
              Semester
            </DialogTitle>
          </DialogHeader>
          {termDraft && (
            <div className="space-y-3">
              <div className="space-y-1.5">
                <Label htmlFor="term-code">
                  {knownCodes.has(termDraft.code)
                    ? "Code — from the courses clients have uploaded"
                    : "Code — the school's own, e.g. 1151 or 115H"}
                </Label>
                <Input
                  id="term-code"
                  value={termDraft.code}
                  onChange={(e) =>
                    setTermDraft({ ...termDraft, code: e.target.value.trim() })
                  }
                  placeholder="1151"
                  readOnly={knownCodes.has(termDraft.code)}
                />
              </div>
              <div className="grid grid-cols-2 gap-3">
                <div className="space-y-1.5">
                  <Label htmlFor="term-start">First day of classes</Label>
                  <Input
                    id="term-start"
                    type="date"
                    value={termDraft.start_date}
                    onChange={(e) =>
                      setTermDraft({ ...termDraft, start_date: e.target.value })
                    }
                  />
                </div>
                <div className="space-y-1.5">
                  <Label htmlFor="term-end">Last day of classes</Label>
                  <Input
                    id="term-end"
                    type="date"
                    value={termDraft.end_date}
                    onChange={(e) =>
                      setTermDraft({ ...termDraft, end_date: e.target.value })
                    }
                  />
                </div>
              </div>
              <p className="text-xs text-muted-foreground">
                Both dates are inclusive. The apps show "start of" and "end of"
                events on these days; they do not silence reminders on their
                own — only a holiday does that.
              </p>
            </div>
          )}
          <DialogFooter>
            <Button variant="outline" onClick={() => setTermDraft(null)}>
              Cancel
            </Button>
            <Button
              disabled={
                !termDraft?.code ||
                !termDraft?.start_date ||
                !termDraft?.end_date ||
                saveTerm.isPending
              }
              onClick={() => termDraft && saveTerm.mutate(termDraft)}
            >
              Save
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>

      <Dialog
        open={holidayDraft !== null}
        onOpenChange={(o) => !o && setHolidayDraft(null)}
      >
        <DialogContent>
          <DialogHeader>
            <DialogTitle>Holiday</DialogTitle>
          </DialogHeader>
          {holidayDraft && (
            <div className="space-y-3">
              <div className="grid grid-cols-2 gap-3">
                <div className="space-y-1.5">
                  <Label htmlFor="holiday-zh">Name (Chinese)</Label>
                  <Input
                    id="holiday-zh"
                    value={holidayDraft.name_zh}
                    onChange={(e) =>
                      setHolidayDraft({
                        ...holidayDraft,
                        name_zh: e.target.value,
                      })
                    }
                    placeholder="中秋節"
                  />
                </div>
                <div className="space-y-1.5">
                  <Label htmlFor="holiday-en">Name (English)</Label>
                  <Input
                    id="holiday-en"
                    value={holidayDraft.name_en}
                    onChange={(e) =>
                      setHolidayDraft({
                        ...holidayDraft,
                        name_en: e.target.value,
                      })
                    }
                    placeholder="Mid-Autumn Festival"
                  />
                </div>
              </div>
              <div className="grid grid-cols-2 gap-3">
                <div className="space-y-1.5">
                  <Label htmlFor="holiday-start">First day</Label>
                  <Input
                    id="holiday-start"
                    type="date"
                    value={holidayDraft.start_date}
                    onChange={(e) =>
                      setHolidayDraft({
                        ...holidayDraft,
                        start_date: e.target.value,
                        // A one-day holiday is the common case; mirroring the
                        // start saves the second click and is overwritten the
                        // moment the operator wants a range.
                        end_date: holidayDraft.end_date || e.target.value,
                      })
                    }
                  />
                </div>
                <div className="space-y-1.5">
                  <Label htmlFor="holiday-end">Last day</Label>
                  <Input
                    id="holiday-end"
                    type="date"
                    value={holidayDraft.end_date}
                    onChange={(e) =>
                      setHolidayDraft({
                        ...holidayDraft,
                        end_date: e.target.value,
                      })
                    }
                  />
                </div>
              </div>
              <p className="text-xs text-muted-foreground">
                Both names are required — the holiday name is the whole content
                of the calendar row, in whichever language the student reads.
                Class reminders, the live chip and the next-class widgets all
                go quiet on these days unless a student opts back in.
              </p>
            </div>
          )}
          <DialogFooter>
            <Button variant="outline" onClick={() => setHolidayDraft(null)}>
              Cancel
            </Button>
            <Button
              disabled={
                !holidayDraft?.name_zh ||
                !holidayDraft?.name_en ||
                !holidayDraft?.start_date ||
                !holidayDraft?.end_date ||
                saveHoliday.isPending
              }
              onClick={() => holidayDraft && saveHoliday.mutate(holidayDraft)}
            >
              Save
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </div>
  );
}
