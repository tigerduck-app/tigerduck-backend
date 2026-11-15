// One announcement: its text with the evidence marked, behind the reveal;
// the classifier's exam and class scores; the newest extraction's checks,
// changes, exams and make-up classes; the codes it skipped; Re-extract.

import type { ReactNode } from "react";
import { Check as CheckIcon, ExternalLink, Paperclip, RefreshCw, Send, TriangleAlert, X } from "lucide-react";
import { dayDate } from "@/lib/academic";
import { cn } from "@/lib/cn";
import { monthDayTime } from "@/lib/format";
import { Pill } from "@/components/page";
import { Button } from "@/components/ui/button";
import { meetsText, type Change, type Course } from "../course-changes/model";
import { ChangeDetail, ItemActions, KindPills, type ItemHandlers } from "./items";
import {
  asChange,
  checkNote,
  CLASS_LABELS,
  EXAM_KIND,
  markRuns,
  mergedChecks,
  STATE_PILL,
  type Detail,
  type DetailItem,
  type Item,
} from "./model";

const BAND = "[background:repeating-linear-gradient(135deg,transparent_0_4px,rgb(91_97_107/0.14)_4px_6px)]";

export function DetailPanel({
  detail,
  courses,
  changes,
  showPii,
  onShowPii,
  onSelect,
  canEdit,
  handlers,
  onReextract,
  reextracting,
}: {
  detail: Detail;
  courses: ReadonlyMap<string, Course>;
  changes: ReadonlyMap<number, Change>;
  showPii: boolean;
  onShowPii: () => void;
  onSelect: (id: string) => void;
  canEdit: (item: Item) => boolean;
  handlers: ItemHandlers;
  onReextract: () => void;
  reextracting: boolean;
}) {
  const course = detail.course_codes.map((c) => courses.get(c)).find(Boolean);
  const changeItems = detail.items.filter((i) => i.kind === "course_change");
  const unique = (kind: DetailItem["kind"]) => [
    ...new Map(
      detail.items.filter((i) => i.kind === kind).map((i) => [JSON.stringify(i.payload), i.payload]),
    ).values(),
  ];
  const exams = unique("exam");
  const makeups = unique("makeup");
  const edited = detail.modified_at !== detail.posted_at;
  return (
    <section
      aria-labelledby="detail-h"
      className="flex min-w-0 flex-col gap-[18px] rounded-[14px] border border-line bg-surface px-6 py-5 phone:px-4"
    >
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div className="flex min-w-0 flex-col gap-0.5">
          <span className="font-mono text-[13px] text-ink-3">{detail.course_codes.join(", ")}</span>
          <h2 id="detail-h" className="break-words text-xl font-semibold leading-snug">
            {detail.subject}
          </h2>
          {course && (
            <span className="mt-0.5 text-[13px] text-ink-2">
              {course.name} · {meetsText(course)}
            </span>
          )}
          <span className="flex flex-wrap items-center gap-x-1.5 text-[13px] text-ink-3">
            <span>
              Posted {monthDayTime(detail.posted_at)}
              {edited && ` · Edited ${monthDayTime(detail.modified_at)}`}
            </span>
            {detail.has_attachment && (
              <span className="inline-flex items-center gap-1">
                · <Paperclip className="h-3.5 w-3.5" />
                Attachment
              </span>
            )}
          </span>
        </div>
        <Button asChild variant="line" size="bar">
          <a href={detail.moodle_url} target="_blank" rel="noreferrer">
            Moodle
            <ExternalLink />
          </a>
        </Button>
      </div>

      <Body detail={detail} showPii={showPii} onShowPii={onShowPii} />

      <div className="grid grid-cols-2 gap-4 phone:grid-cols-1">
        <Scores detail={detail} />
        <Extraction detail={detail} items={changeItems} course={course} changes={changes} onSelect={onSelect} />
      </div>

      {changeItems.length > 0 && (
        <Section title="Changes">
          {changeItems.map((item) => {
            const existing = item.existing_change_id === null ? undefined : changes.get(item.existing_change_id);
            const state = STATE_PILL[item.state];
            return (
              <div key={item.id} className="flex flex-wrap items-center gap-2.5 rounded-xl border border-line px-3 py-2.5">
                <KindPills item={item} term={detail.term} />
                <ChangeDetail item={item} term={detail.term} course={courses.get(item.course_no)} existing={existing} />
                <span className="ml-auto inline-flex items-center gap-1">
                  {item.state === "pending" ? (
                    <ItemActions item={item} {...handlers} onEdit={canEdit(item) ? handlers.onEdit : undefined} />
                  ) : (
                    state && <Pill tone={state[1]}>{state[0]}</Pill>
                  )}
                </span>
              </div>
            );
          })}
        </Section>
      )}

      {exams.length > 0 && (
        <Section title="Exams">
          {exams.map((e, n) => (
            <Fact key={n} label={EXAM_KIND[String(e.kind)] ?? EXAM_KIND.other} fact={e} extra={e.scope} />
          ))}
        </Section>
      )}
      {makeups.length > 0 && (
        <Section title="Make-up classes">
          {makeups.map((m, n) => (
            <Fact key={n} fact={m} />
          ))}
        </Section>
      )}
      {detail.skipped_codes.length > 0 && detail.model && (
        <Section title="Skipped codes">
          <div className="flex flex-wrap gap-1.5">
            {detail.skipped_codes.map((c) => (
              <span key={c} className="rounded-full border border-line bg-ground px-2 py-px font-mono text-xs text-ink-2">
                {c}
              </span>
            ))}
          </div>
        </Section>
      )}

      <div className="flex justify-end border-t border-line pt-3.5">
        <Button variant="line" size="bar" disabled={reextracting || detail.route !== "sent"} onClick={onReextract}>
          <RefreshCw />
          Re-extract
        </Button>
      </div>
    </section>
  );
}

function Section({ title, children }: { title: string; children: ReactNode }) {
  return (
    <div className="flex flex-col gap-1.5">
      <h3 className="text-[13px] font-semibold">{title}</h3>
      {children}
    </div>
  );
}

function Fact({ label, fact, extra }: { label?: string; fact: Record<string, unknown>; extra?: unknown }) {
  const date = typeof fact.date === "string" ? fact.date : null;
  const time = [fact.start_time, fact.end_time].filter((t) => typeof t === "string").join("–");
  return (
    <div className="flex flex-wrap items-center gap-2.5 rounded-xl border border-line px-3 py-2.5">
      {label && <Pill tone="info">{label}</Pill>}
      {date && <span className="font-medium">{dayDate(date)}</span>}
      {time && <span className="font-mono text-xs text-ink-2">{time}</span>}
      {typeof fact.room === "string" && <span className="font-mono text-xs text-ink-2">{fact.room}</span>}
      {typeof extra === "string" && <span className="text-[13px] text-ink-3">{extra}</span>}
    </div>
  );
}

function Body({ detail, showPii, onShowPii }: { detail: Detail; showPii: boolean; onShowPii: () => void }) {
  const box = "rounded-xl bg-ground px-4 py-3.5 text-sm leading-[1.65]";
  if (!showPii) {
    return (
      <div className={cn(box, "flex items-center justify-between gap-3 text-ink-3")}>
        <span>Text hidden</span>
        <Button variant="line" size="sm" onClick={onShowPii}>
          Show
        </Button>
      </div>
    );
  }
  if (detail.text === null) return null;
  // The text is the title, a blank line and the body; the heading shows the title.
  const cut = detail.text.indexOf("\n\n");
  const from = cut < 0 ? 0 : cut + 2;
  const spans = detail.items.map((i) => i.evidence ?? "");
  const runs = markRuns(detail.text, spans);
  let at = 0;
  return (
    <div className={cn(box, "whitespace-pre-wrap break-words text-ink")}>
      {runs.map((r, n) => {
        const start = at;
        at += r.text.length;
        const text = start >= from ? r.text : r.text.slice(from - start);
        if (!text) return null;
        return r.mark ? (
          <mark key={n} className="rounded-[3px] bg-[#FFE7A3] px-0.5 text-[#15171A]">
            {text}
          </mark>
        ) : (
          <span key={n}>{text}</span>
        );
      })}
    </div>
  );
}

function Scores({ detail }: { detail: Detail }) {
  const scores = detail.scores ?? {};
  const bars = CLASS_LABELS.map((label) => [label, scores[label] ?? 0] as const).sort((a, b) => b[1] - a[1]);
  return (
    <div className="flex min-w-0 flex-col gap-2.5">
      <div className="flex items-baseline justify-between gap-2">
        <h3 className="text-[13px] font-semibold">Classifier</h3>
        {detail.classifier_release && (
          <span className="truncate font-mono text-[11px] text-ink-3">{detail.classifier_release}</span>
        )}
      </div>
      {detail.scores &&
        bars.map(([label, score]) => {
          const above = detail.labels.includes(label);
          return (
            <div key={label} className="grid grid-cols-[112px_minmax(0,1fr)_36px] items-center gap-2.5">
              <span className={cn("truncate font-mono text-xs", above ? "text-ink" : "text-ink-3")}>{label}</span>
              <div className="relative h-2 overflow-hidden rounded-full bg-soft">
                <div className={cn("absolute inset-y-0 left-[10%] w-[80%]", BAND)} />
                <div
                  style={{ width: `${Math.max(score * 100, 1)}%` }}
                  className={cn("absolute inset-y-0 left-0 rounded-full", above ? "bg-chart-1" : "bg-disabled")}
                />
              </div>
              <span className="text-right font-mono text-xs text-ink-2">{score.toFixed(2)}</span>
            </div>
          );
        })}
      {detail.scores && (
        <div className="flex gap-3.5 text-[11px] text-ink-3">
          <span className="inline-flex items-center gap-1.5">
            <span className={cn("h-2 w-[22px] rounded-full bg-soft", BAND)} />
            0.10–0.90
          </span>
        </div>
      )}
    </div>
  );
}

function Extraction({
  detail,
  items,
  course,
  changes,
  onSelect,
}: {
  detail: Detail;
  items: Item[];
  course: Course | undefined;
  changes: ReadonlyMap<number, Change>;
  onSelect: (id: string) => void;
}) {
  const latest = detail.extraction;
  const checks = mergedChecks(items);
  const empty = (icon: ReactNode, title: string) => (
    <div className="flex flex-col items-center gap-2 px-2 py-5 text-center">
      <span aria-hidden="true" className="flex size-11 items-center justify-center rounded-full bg-ground text-ink-3">
        {icon}
      </span>
      <span className="text-[15px] font-semibold">{title}</span>
    </div>
  );
  return (
    <div className="flex min-w-0 flex-col gap-2.5">
      <div className="flex items-baseline justify-between gap-2">
        <h3 className="text-[13px] font-semibold">Extraction</h3>
        {detail.model && <span className="truncate font-mono text-[11px] text-ink-3">{detail.model}</span>}
      </div>
      {latest?.status === "failed" && (
        <div className="flex items-start gap-2.5 rounded-[10px] bg-badge-danger px-3.5 py-3 text-[13px] text-badge-danger-fg">
          <TriangleAlert className="mt-0.5 h-4 w-4 shrink-0" />
          <span>
            Extraction failed after {latest.attempts} {latest.attempts === 1 ? "attempt" : "attempts"} ·{" "}
            {monthDayTime(latest.updated_at)}
          </span>
        </div>
      )}
      {detail.route === "unclassified" && empty(<Send className="h-5 w-5" />, "Not classified")}
      {detail.route === "not_sent" && !latest && empty(<Send className="h-5 w-5" />, "Not sent")}
      {!detail.model && (latest?.status === "queued" || latest?.status === "running") &&
        empty(<RefreshCw className="h-5 w-5" />, "Queued")}
      {detail.model && checks.length === 0 && items.length === 0 && (
        <span className="text-[13px] text-ink-3">No changes</span>
      )}
      {checks.length > 0 && (
        <div className="flex flex-col">
          {checks.map((c) => {
            const change = asChange(c.item, detail.term);
            const note = checkNote(c, change, course, c.clash === null ? undefined : changes.get(c.clash), detail.labels);
            return (
              <div key={c.code} className="flex min-h-[26px] items-center gap-2 text-[13px]">
                {c.ok ? (
                  <CheckIcon className="h-[15px] w-[15px] shrink-0 text-dot-ok" strokeWidth={2.4} />
                ) : (
                  <X className="h-[15px] w-[15px] shrink-0 text-dot-bad" strokeWidth={2.4} />
                )}
                <span className="text-ink-2">{c.label}</span>
                {c.by ? (
                  <button
                    type="button"
                    onClick={() => onSelect(c.by!)}
                    className="ml-auto font-mono text-xs text-link hover:text-link-hover"
                  >
                    {c.by}
                  </button>
                ) : (
                  <span className="ml-auto text-right text-xs text-ink-3">{note}</span>
                )}
              </div>
            );
          })}
        </div>
      )}
    </div>
  );
}
