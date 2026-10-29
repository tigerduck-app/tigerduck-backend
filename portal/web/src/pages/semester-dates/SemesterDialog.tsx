import { useState } from "react";
import { ChevronLeft, ChevronRight, Trash2 } from "lucide-react";
import { Button } from "@/components/ui/button";
import { ErrorAlert, FormDialog, fieldClass } from "@/components/page";
import { cn } from "@/lib/cn";
import { daysBetween, longDate, termLabel, todayIso, toIso, weeks } from "@/lib/academic";

export type SemesterDraft = {
  kind: "term" | "holiday";
  /** A term's code when editing; a holiday's id. */
  code?: string;
  id?: number;
  nameZh?: string;
  nameEn?: string;
  from?: string;
  to?: string;
};

export type SemesterSave = {
  code: string;
  name_zh: string;
  name_en: string;
  start_date: string;
  end_date: string;
};

const MONTH_NAMES = [
  "January", "February", "March", "April", "May", "June",
  "July", "August", "September", "October", "November", "December",
];

/** Adds or edits a term (`kind: "term"`) or a holiday; `editing` picks the mode. */
export function SemesterDialog({
  draft,
  editing,
  codes,
  marks,
  error,
  pending,
  onClose,
  onSave,
  onDelete,
}: {
  draft: SemesterDraft;
  editing: boolean;
  /** Undated codes offered for a new term. */
  codes: readonly string[];
  /** Holidays for a term, terms for a holiday: dotted in the calendar. */
  marks: readonly { from: string; to: string }[];
  error: string | null;
  pending: boolean;
  onClose: () => void;
  onSave: (value: SemesterSave) => void;
  onDelete: () => void;
}) {
  const isTerm = draft.kind === "term";
  const [code, setCode] = useState(draft.code ?? "");
  const [other, setOther] = useState(!editing && isTerm && !codes.includes(draft.code ?? ""));
  const [nameZh, setNameZh] = useState(draft.nameZh ?? "");
  const [nameEn, setNameEn] = useState(draft.nameEn ?? "");
  const [from, setFrom] = useState<string | null>(draft.from ?? null);
  const [picked, setPicked] = useState<string | null>(draft.to ?? null);
  const [tried, setTried] = useState(false);
  const today = todayIso();
  const start = draft.from ?? today;
  const [view, setView] = useState({ y: Number(start.slice(0, 4)), m: Number(start.slice(5, 7)) - 1 });

  // A holiday of one day needs one pick.
  const to = picked ?? (isTerm ? null : from);
  const awaitingEnd = from !== null && picked === null;
  const pick = (day: string) => {
    if (!from || picked) {
      setFrom(day);
      setPicked(null);
    } else if (day < from) {
      setPicked(from);
      setFrom(day);
    } else {
      setPicked(day);
    }
  };
  const shift = (n: number) =>
    setView(({ y, m }) => ({ y: y + Math.floor((m + n) / 12), m: (((m + n) % 12) + 12) % 12 }));

  const zhMissing = !isTerm && tried && !nameZh.trim();
  const enMissing = !isTerm && tried && !nameEn.trim();
  const disabled = pending || !(isTerm ? from && to : from) || (isTerm && !code.trim());
  const span = from && to ? daysBetween(from, to) + 1 : 0;

  const save = () => {
    if (!isTerm && (!nameZh.trim() || !nameEn.trim())) {
      setTried(true);
      return;
    }
    onSave({
      code: code.trim(),
      name_zh: nameZh.trim(),
      name_en: nameEn.trim(),
      start_date: from!,
      end_date: to ?? from!,
    });
  };

  return (
    <FormDialog
      open
      onOpenChange={(open) => !open && onClose()}
      width={720}
      title={
        isTerm
          ? editing ? `Edit ${termLabel(code)}` : "Add term"
          : editing ? "Edit holiday" : "Add holiday"
      }
      meta={isTerm ? "First and last day of classes, inclusive" : "Classes do not meet on these days"}
      footer={
        <>
          {editing && (
            <Button variant="quiet" size="form" className="mr-auto px-3 text-danger hover:text-danger" onClick={onDelete}>
              <Trash2 className="!size-[15px]" />
              Delete
            </Button>
          )}
          <Button variant="line" size="form" onClick={onClose}>
            Cancel
          </Button>
          <Button variant="ink" size="form" disabled={disabled} onClick={save}>
            Save
          </Button>
        </>
      }
    >
      {error && <ErrorAlert>{error}</ErrorAlert>}

      {isTerm && !editing && (
        <div className="flex flex-col gap-1.5">
          <span id="sd-code" className="text-[13px] font-medium">
            Term
          </span>
          <div role="radiogroup" aria-labelledby="sd-code" className="flex flex-wrap gap-2">
            {codes.map((c) => (
              <CodeOption
                key={c}
                checked={!other && code === c}
                label={termLabel(c)}
                note="Courses filed, no dates"
                onPick={() => {
                  setCode(c);
                  setOther(false);
                }}
              />
            ))}
            <CodeOption
              checked={other}
              label="Other code"
              note="Type it"
              onPick={() => {
                setOther(true);
                setCode("");
              }}
            />
          </div>
          {other && (
            <label className="flex max-w-[220px] flex-col gap-1.5 text-[13px] font-medium">
              Code
              <input
                value={code}
                onChange={(e) => setCode(e.target.value.trim())}
                placeholder="1152"
                maxLength={8}
                autoFocus
                className={cn(fieldClass, "font-mono font-normal")}
              />
            </label>
          )}
        </div>
      )}

      {!isTerm && (
        <div className="flex flex-wrap gap-4">
          <NameField label="Name (Chinese)" value={nameZh} onChange={setNameZh} placeholder="中秋節" missing={zhMissing} />
          <NameField label="Name (English)" value={nameEn} onChange={setNameEn} placeholder="Mid-Autumn Festival" missing={enMissing} />
        </div>
      )}

      <div className="flex flex-wrap gap-3">
        <DateBox label={isTerm ? "First day of classes" : "First day"} value={from} active={!from} />
        <DateBox label={isTerm ? "Last day of classes" : "Last day"} value={to} active={awaitingEnd} />
        <div className="flex flex-col justify-center gap-0.5 px-3.5 py-2.5">
          <span className="text-xs text-ink-3">Length</span>
          <span className="text-[15px] font-semibold tabular-nums">
            {span ? (isTerm ? `${weeks(from!, to!)} weeks` : `${span} ${span === 1 ? "day" : "days"}`) : "—"}
          </span>
        </div>
      </div>

      <div className="rounded-xl border border-line px-3.5 pb-3.5 pt-3">
        <div className="mb-2 flex items-center justify-between">
          <Button variant="quiet" size="icon" aria-label="Previous month" className="rounded-lg text-ink-2" onClick={() => shift(-1)}>
            <ChevronLeft />
          </Button>
          <span className="text-xs text-ink-3">
            {!from
              ? "Pick the first day"
              : awaitingEnd
                ? isTerm ? "Pick the last day" : "Pick the last day, or save one day"
                : "Pick again to start over"}
          </span>
          <Button variant="quiet" size="icon" aria-label="Next month" className="rounded-lg text-ink-2" onClick={() => shift(1)}>
            <ChevronRight />
          </Button>
        </div>
        <div className="flex flex-wrap gap-6 max-[720px]:[&>:nth-child(n+2)]:hidden">
          {[0, 1].map((k) => (
            <Month
              key={k}
              y={view.y + Math.floor((view.m + k) / 12)}
              m={(view.m + k) % 12}
              from={from}
              to={to}
              today={today}
              marked={(day) => marks.some((r) => day >= r.from && day <= r.to)}
              markClass={isTerm ? "bg-dot-warn" : "bg-link"}
              onPick={pick}
            />
          ))}
        </div>
        <div className="mt-2.5 flex flex-wrap gap-3.5 text-xs text-ink-2">
          <span className="flex items-center gap-1.5">
            <span aria-hidden="true" className={cn("size-1.5 rounded-full", isTerm ? "bg-dot-warn" : "bg-link")} />
            {isTerm ? "Holiday" : "Classes"}
          </span>
          <span className="flex items-center gap-1.5">
            <span aria-hidden="true" className="size-3.5 rounded border-[1.5px] border-ink" />
            Today
          </span>
        </div>
      </div>
    </FormDialog>
  );
}

function CodeOption({
  checked,
  label,
  note,
  onPick,
}: {
  checked: boolean;
  label: string;
  note: string;
  onPick: () => void;
}) {
  return (
    <button
      type="button"
      role="radio"
      aria-checked={checked}
      onClick={onPick}
      className={cn(
        "flex min-h-14 flex-col items-start justify-center gap-0.5 rounded-xl border-2 px-3.5 py-2 text-left",
        checked ? "border-link bg-selected" : "border-line bg-surface hover:border-field",
      )}
    >
      <span className="font-semibold">{label}</span>
      <span className="text-xs text-ink-3">{note}</span>
    </button>
  );
}

function NameField({
  label,
  value,
  onChange,
  placeholder,
  missing,
}: {
  label: string;
  value: string;
  onChange: (v: string) => void;
  placeholder: string;
  missing: boolean;
}) {
  return (
    <label className="flex flex-[1_1_220px] flex-col gap-1.5 text-[13px] font-medium">
      {label}
      <input
        value={value}
        onChange={(e) => onChange(e.target.value)}
        placeholder={placeholder}
        aria-invalid={missing}
        className={cn(fieldClass, "font-normal", missing && "border-dot-bad")}
      />
      {missing && <span className="text-xs font-normal text-badge-danger-fg">Required</span>}
    </label>
  );
}

function DateBox({ label, value, active }: { label: string; value: string | null; active: boolean }) {
  return (
    <div
      className={cn(
        "flex flex-[1_1_160px] flex-col gap-0.5 rounded-[10px] bg-ground px-3.5 py-2.5",
        active && "ring-2 ring-inset ring-link",
      )}
    >
      <span className="text-xs text-ink-3">{label}</span>
      <span className="text-[15px] font-semibold tabular-nums">{value ? longDate(value) : "—"}</span>
    </div>
  );
}

function Month({
  y,
  m,
  from,
  to,
  today,
  marked,
  markClass,
  onPick,
}: {
  y: number;
  m: number;
  from: string | null;
  to: string | null;
  today: string;
  marked: (day: string) => boolean;
  markClass: string;
  onPick: (day: string) => void;
}) {
  const lead = new Date(Date.UTC(y, m, 1)).getUTCDay();
  const count = new Date(Date.UTC(y, m + 1, 0)).getUTCDate();
  const title = `${MONTH_NAMES[m]} ${y}`;
  return (
    <div className="min-w-0 flex-[1_1_260px]">
      <div className="mb-1.5 text-center text-sm font-semibold">{title}</div>
      <div role="grid" aria-label={title} className="grid grid-cols-7 gap-y-0.5">
        {["Su", "Mo", "Tu", "We", "Th", "Fr", "Sa"].map((w) => (
          <span key={w} className="py-1 text-center text-[11px] text-ink-3">
            {w}
          </span>
        ))}
        {Array.from({ length: lead }, (_, i) => (
          <div key={`lead-${i}`} className="h-[38px]" />
        ))}
        {Array.from({ length: count }, (_, i) => {
          const day = toIso(Date.UTC(y, m, i + 1));
          const end = from !== null && (day === from || day === to);
          const inside = from !== null && to !== null && day > from && day < to;
          const ranged = from !== null && to !== null && from !== to;
          return (
            <div
              key={day}
              className={cn(
                "flex h-[38px]",
                inside && "bg-badge-info",
                ranged && day === from && "bg-[linear-gradient(to_right,transparent_50%,var(--color-badge-info)_50%)]",
                ranged && day === to && "bg-[linear-gradient(to_left,transparent_50%,var(--color-badge-info)_50%)]",
              )}
            >
              <button
                type="button"
                aria-label={longDate(day)}
                aria-pressed={end}
                onClick={() => onPick(day)}
                className={cn(
                  "mx-px flex flex-1 flex-col items-center justify-center gap-px rounded-lg text-[13px] tabular-nums",
                  end ? "bg-link font-semibold text-on-ink" : "text-ink hover:bg-soft",
                  inside && "rounded-none hover:bg-transparent",
                  day === today && "ring-[1.5px] ring-inset ring-ink",
                )}
              >
                <span>{i + 1}</span>
                <span
                  aria-hidden="true"
                  className={cn("size-1 rounded-full", marked(day) ? (end ? "bg-on-ink" : markClass) : "bg-transparent")}
                />
              </button>
            </div>
          );
        })}
      </div>
    </div>
  );
}
