// The student's sync log as it is written, polled every 2 s.

import { useEffect, useLayoutEffect, useRef, useState } from "react";
import { Pause, Play } from "lucide-react";
import { api } from "@/lib/api";
import { Pill } from "@/components/page";
import { cn } from "@/lib/cn";
import { Chips } from "./controls";
import { clock, errorText } from "./format";
import type { LogEntry } from "./types";

const KEEP = 500;
const LEVEL: Record<string, string> = {
  INFO: "bg-badge-neutral text-badge-neutral-fg",
  WARN: "bg-badge-warning text-badge-warning-fg",
  ERROR: "bg-badge-danger text-badge-danger-fg",
};
type Level = "all" | "warn" | "error";

export function LiveLog({ studentId }: { studentId: string }) {
  const [entries, setEntries] = useState<LogEntry[]>([]);
  const [fresh, setFresh] = useState<Set<number>>(new Set());
  const [paused, setPaused] = useState(false);
  const [level, setLevel] = useState<Level>("all");
  const [hidden, setHidden] = useState<Set<string>>(new Set());
  const [failure, setFailure] = useState<string | null>(null);
  const latest = useRef(0);
  const box = useRef<HTMLDivElement>(null);
  const stick = useRef(true);

  useEffect(() => {
    if (paused) return;
    let alive = true;
    let busy = false;
    const timers: number[] = [];
    const tick = async () => {
      if (busy) return;
      busy = true;
      try {
        const r = await api<{ entries: LogEntry[]; latest_id: number }>(
          `/api/moodle/sync-logs?student_id=${encodeURIComponent(studentId)}&after_id=${latest.current}`,
        );
        if (!alive) return;
        setFailure(null);
        if (r.entries.length === 0) return;
        const first = latest.current === 0;
        latest.current = r.latest_id;
        setEntries((prev) => [...prev, ...r.entries].slice(-KEEP));
        if (!first) {
          const ids = r.entries.map((e) => e.id);
          setFresh((s) => new Set([...s, ...ids]));
          timers.push(
            window.setTimeout(() => setFresh((s) => new Set([...s].filter((id) => !ids.includes(id)))), 2000),
          );
        }
      } catch (e) {
        // The next tick asks again; until one answers, say the log is down rather than empty.
        if (alive) setFailure(errorText(e));
      } finally {
        busy = false;
      }
    };
    void tick();
    const id = window.setInterval(tick, 2000);
    return () => {
      alive = false;
      window.clearInterval(id);
      timers.forEach(clearTimeout);
    };
  }, [studentId, paused]);

  useLayoutEffect(() => {
    if (stick.current && box.current) box.current.scrollTop = box.current.scrollHeight;
  }, [entries, level, hidden]);

  const sources = [...new Set(entries.map((e) => e.source))].sort();
  const warn = entries.filter((e) => e.level !== "INFO").length;
  const errors = entries.filter((e) => e.level === "ERROR").length;
  const shown = entries.filter(
    (e) =>
      !hidden.has(e.source) &&
      (level === "all" || (level === "warn" ? e.level !== "INFO" : e.level === "ERROR")),
  );

  return (
    <section aria-labelledby="log-h" className="min-w-0 rounded-[14px] border border-line bg-surface">
      <div className="flex flex-wrap items-center gap-3 border-b border-line px-6 py-3.5 phone:px-4">
        <h2 id="log-h" className="text-[15px] font-semibold">
          Live sync log
        </h2>
        <Pill tone={paused ? "neutral" : "success"} dot={paused ? "off" : "ok"}>
          {paused ? "Paused" : "Live · every 2 s"}
        </Pill>
        <Chips
          label="Level"
          className="ml-auto phone:ml-0"
          isOn={(l) => l === level}
          onPick={setLevel}
          options={[
            { value: "all", label: "All", count: entries.length },
            { value: "warn", label: "Warnings", count: warn },
            { value: "error", label: "Errors", count: errors },
          ]}
        />
        {sources.length > 0 && (
          <Chips
            label="Source"
            mono
            className="phone:hidden"
            isOn={(s) => !hidden.has(s)}
            onPick={(s) =>
              setHidden((h) => {
                const next = new Set(h);
                if (next.has(s)) next.delete(s);
                else next.add(s);
                return next;
              })
            }
            options={sources.map((s) => ({ value: s, label: s }))}
          />
        )}
        <button
          type="button"
          onClick={() => setPaused((p) => !p)}
          className="flex min-h-8 items-center gap-1.5 rounded-[9px] border border-field bg-surface px-3 text-[13px] hover:bg-row-hover"
        >
          {paused ? <Play className="h-3.5 w-3.5" /> : <Pause className="h-3.5 w-3.5" />}
          {paused ? "Resume" : "Pause"}
        </button>
      </div>
      <div
        ref={box}
        role="log"
        aria-live="polite"
        onScroll={(e) => {
          const el = e.currentTarget;
          stick.current = el.scrollHeight - el.scrollTop - el.clientHeight < 40;
        }}
        className="h-80 overflow-y-auto rounded-b-[14px] bg-row-hover py-2 font-mono text-xs"
      >
        {shown.map((e) => (
          <div
            key={e.id}
            className={cn(
              "grid grid-cols-[72px_60px_76px_160px_minmax(0,1fr)] items-baseline gap-2 px-6 py-[3px] transition-colors duration-1000 phone:grid-cols-[62px_52px_minmax(0,1fr)] phone:px-4",
              fresh.has(e.id) && "bg-badge-warning",
            )}
          >
            <span className="text-ink-3">{clock(e.ts, true)}</span>
            <span className={cn("justify-self-start rounded px-1.5 text-[11px] font-medium", LEVEL[e.level] ?? LEVEL.INFO)}>
              {e.level}
            </span>
            <span className="truncate text-badge-info-fg phone:hidden">{e.source}</span>
            <span className="truncate text-badge-violet-fg phone:hidden">
              {e.device_label ? `[${e.platform ?? "?"}/${e.device_label.slice(0, 8)}]` : ""}
            </span>
            <span className="min-w-0 text-ink [overflow-wrap:anywhere]">{e.message}</span>
          </div>
        ))}
        {failure ? (
          <div role="alert" className="flex flex-col items-center gap-1 p-10 text-center font-sans text-[13px]">
            <span className="font-semibold text-badge-danger-fg">Log unavailable</span>
            <span className="break-all text-ink-3">{failure}</span>
          </div>
        ) : (
          shown.length === 0 && <div className="p-10 text-center font-sans text-[13px] text-ink-3">No lines at this level</div>
        )}
      </div>
    </section>
  );
}
