// The person picker: part of a student ID or a name, and a list to pick
// from, since several students can share a name.

import { useEffect, useState, type KeyboardEvent } from "react";
import { useQuery } from "@tanstack/react-query";
import { Search } from "lucide-react";
import { api } from "@/lib/api";
import { cn } from "@/lib/cn";
import { ago, errorText } from "./format";
import type { StudentMatch } from "./types";

function Marked({ text, term, className }: { text: string; term: string; className?: string }) {
  const at = term ? text.toLowerCase().indexOf(term.toLowerCase()) : -1;
  if (at < 0) return <span className={className}>{text}</span>;
  return (
    <span className={className}>
      {text.slice(0, at)}
      <mark className="rounded-[3px] bg-[#FFE7A3] text-[#15171A]">{text.slice(at, at + term.length)}</mark>
      {text.slice(at + term.length)}
    </span>
  );
}

export function StudentSearch({
  value,
  onChange,
  onPick,
}: {
  value: string;
  onChange: (v: string) => void;
  onPick: (studentId: string) => void;
}) {
  const [open, setOpen] = useState(false);
  const [active, setActive] = useState(0);
  const [term, setTerm] = useState(value.trim());
  // Waits for a pause in typing before asking the server.
  useEffect(() => {
    const t = setTimeout(() => setTerm(value.trim()), 250);
    return () => clearTimeout(t);
  }, [value]);

  const matchesQ = useQuery({
    queryKey: ["moodle", "student-search", term],
    queryFn: () => api<{ students: StudentMatch[] }>(`/api/moodle/student-search?q=${encodeURIComponent(term)}`),
    enabled: open && term.length > 0,
    staleTime: 30_000,
  });
  const failed = matchesQ.isError;
  const matches = failed ? [] : matchesQ.data?.students ?? [];
  // Until the pause in typing, the results are for an older input; Enter there would open the wrong student.
  const shown = open && value.trim().length > 0 && term === value.trim() && (!!matchesQ.data || failed);

  const pick = (m: StudentMatch) => {
    setOpen(false);
    onPick(m.student_id);
  };
  const onKey = (e: KeyboardEvent<HTMLInputElement>) => {
    if (e.key === "Escape") setOpen(false);
    if (!shown || matches.length === 0) return;
    if (e.key === "ArrowDown" || e.key === "ArrowUp") {
      e.preventDefault();
      const step = e.key === "ArrowDown" ? 1 : -1;
      setActive((i) => (i + step + matches.length) % matches.length);
    } else if (e.key === "Enter") {
      e.preventDefault();
      pick(matches[Math.min(active, matches.length - 1)]);
    }
  };

  return (
    <div className="relative min-w-[260px] flex-[0_1_400px] phone:min-w-0 phone:flex-[1_1_100%]">
      <label className="flex h-10 items-center gap-2 rounded-[9px] border border-field bg-surface px-3 text-ink-3">
        <Search className="h-4 w-4 shrink-0" />
        <input
          type="search"
          role="combobox"
          aria-label="Search by student ID or name"
          aria-expanded={shown}
          aria-controls="student-matches"
          aria-activedescendant={shown && matches.length ? `student-match-${active}` : undefined}
          placeholder="Student ID or name"
          value={value}
          onChange={(e) => {
            onChange(e.target.value);
            setOpen(true);
            setActive(0);
          }}
          onFocus={() => setOpen(true)}
          onBlur={() => setOpen(false)}
          onKeyDown={onKey}
          className="min-w-0 flex-1 bg-transparent font-mono text-sm text-ink outline-none placeholder:font-sans placeholder:text-ink-3"
        />
      </label>
      {shown && (
        <div
          id="student-matches"
          role="listbox"
          aria-label="Matching students"
          className="absolute left-0 top-[46px] z-40 w-[480px] max-w-[calc(100vw-48px)] rounded-xl border border-line bg-surface p-1.5 shadow-[0_12px_32px_rgba(20,22,26,0.16)] phone:w-full"
        >
          {failed ? (
            <div role="alert" className="flex items-center justify-between gap-3 px-2.5 py-2 text-[13px] text-badge-danger-fg">
              <span className="min-w-0 break-all">{errorText(matchesQ.error)}</span>
              <button
                type="button"
                onMouseDown={(e) => e.preventDefault()}
                onClick={() => void matchesQ.refetch()}
                className="flex min-h-8 shrink-0 items-center rounded-[9px] border border-badge-danger-fg/30 bg-surface px-3"
              >
                Try again
              </button>
            </div>
          ) : matches.length > 0 ? (
            <div className="px-2.5 pb-1 pt-1.5 text-xs text-ink-3">
              {matches.length} {matches.length === 1 ? "student" : "students"}
            </div>
          ) : (
            <div className="px-2.5 py-3.5 text-[13px] text-ink-3">No student matches</div>
          )}
          {matches.map((m, i) => (
            <div
              key={m.student_id}
              id={`student-match-${i}`}
              role="option"
              aria-selected={i === active}
              // Keeps the input's focus so the blur does not close the list first.
              onMouseDown={(e) => e.preventDefault()}
              onClick={() => pick(m)}
              onMouseEnter={() => setActive(i)}
              className={cn(
                "grid min-h-11 cursor-pointer grid-cols-[104px_minmax(0,1fr)_auto] items-center gap-3 rounded-lg px-2.5 text-left phone:grid-cols-[96px_minmax(0,1fr)]",
                i === active && "bg-selected",
              )}
            >
              <Marked text={m.student_id} term={term} className="whitespace-nowrap font-mono text-[13px]" />
              <Marked text={m.display_name ?? "—"} term={term} className="truncate font-medium" />
              <span className="whitespace-nowrap text-xs tabular-nums text-ink-3 phone:hidden">
                {m.devices} {m.devices === 1 ? "device" : "devices"} · {ago(m.last_seen_at)}
              </span>
            </div>
          ))}
        </div>
      )}
    </div>
  );
}
