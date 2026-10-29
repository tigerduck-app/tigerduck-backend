// Logs: a container's recent lines, each split by /api/logs/data into
// level, logger, event and fields, with a live mode that polls.

import * as React from "react";
import { useQuery } from "@tanstack/react-query";
import { useSearchParams } from "react-router-dom";
import { toast } from "sonner";
import {
  ChevronDown,
  ChevronRight,
  Copy,
  FileText,
  RefreshCw,
  Search,
  Server,
  TriangleAlert,
} from "lucide-react";
import { api } from "@/lib/api";
import { cn } from "@/lib/cn";
import { copyText } from "@/lib/copy";
import { Button } from "@/components/ui/button";
import { Skeleton } from "@/components/ui/skeleton";
import { DataTable, useTable, type Column, type TableModel } from "@/components/data-table";
import { EmptyState, ErrorAlert, PageHeading } from "@/components/page";
import type { LogLevel, LogLine, LogsPayload, LogsTab } from "@/types/api";

type TabsPayload = { tabs: LogsTab[]; default_tail: number; max_tail: number };
type Row = LogLine & { key: string; ms: number | null };

const POLL_MS = 2000;
const LIVE_STORAGE = "tigerduck.portal.logs.live";
const LEVELS: [LogLevel, string][] = [
  ["error", "Error"],
  ["warning", "Warning"],
  ["info", "Info"],
  ["debug", "Debug"],
];
const LEVEL_LABEL = Object.fromEntries(LEVELS) as Record<LogLevel, string>;
const LEVEL_PILL: Record<LogLevel, string> = {
  error: "bg-badge-danger text-badge-danger-fg",
  warning: "bg-badge-warning text-badge-warning-fg",
  info: "bg-badge-info text-badge-info-fg",
  debug: "bg-badge-neutral text-badge-neutral-fg",
};
const LEVEL_DOT: Record<LogLevel, string> = {
  error: "bg-dot-bad",
  warning: "bg-dot-warn",
  info: "bg-chart-1",
  debug: "bg-dot-off",
};
const RANGES: [string, number][] = [
  ["15 min", 15],
  ["1 h", 60],
  ["6 h", 360],
  ["All", 0],
];
const TAILS = [500, 1000, 2000, 5000];

const segment = (on: boolean) =>
  cn(
    "min-h-7 rounded-[7px] px-2.5 text-xs",
    on ? "bg-ink font-medium text-on-ink" : "text-ink hover:bg-surface/70",
  );

export function LogsPage() {
  const tabsQ = useQuery<TabsPayload>({
    queryKey: ["logs-tabs"],
    queryFn: () => api<TabsPayload>("/api/logs/tabs"),
    staleTime: Infinity,
  });
  const [params, setParams] = useSearchParams();
  const tabs = tabsQ.data?.tabs ?? [];
  const active = tabs.find((t) => t.id === params.get("source")) ?? tabs[0];
  const maxTail = tabsQ.data?.max_tail ?? 5000;
  const tail = Math.max(
    1,
    Math.min(maxTail, Number(params.get("tail")) || tabsQ.data?.default_tail || 500),
  );
  const setParam = (key: string, value: string) => {
    const next = new URLSearchParams(params);
    next.set(key, value);
    setParams(next, { replace: true });
  };

  const [live, setLive] = React.useState(() => {
    try {
      return localStorage.getItem(LIVE_STORAGE) === "1";
    } catch {
      return false;
    }
  });
  React.useEffect(() => {
    try {
      localStorage.setItem(LIVE_STORAGE, live ? "1" : "0");
    } catch {
      // Without storage the choice lasts until the page reloads.
    }
  }, [live]);

  const q = useQuery<LogsPayload>({
    queryKey: ["logs-data", active?.id, tail],
    queryFn: () =>
      api<LogsPayload>(`/api/logs/data?source=${encodeURIComponent(active!.id)}&tail=${tail}`),
    enabled: !!active,
    refetchInterval: live ? POLL_MS : false,
    refetchIntervalInBackground: false,
  });
  const refresh = () =>
    q.refetch().then((r) => {
      if (r.data?.ok) toast.success(`Reloaded ${r.data.lines.length} lines`);
    });

  return (
    <>
      <PageHeading
        group="Overview"
        title="Logs"
        actions={
          <>
            <button
              type="button"
              role="switch"
              aria-checked={live}
              onClick={() => setLive(!live)}
              className={cn(
                "inline-flex min-h-9 items-center gap-2 rounded-[9px] border px-3.5",
                live
                  ? "border-dot-ok/40 bg-badge-success font-medium text-badge-success-fg"
                  : "border-field bg-surface text-ink hover:bg-row-hover",
              )}
            >
              <span
                aria-hidden="true"
                className={cn(
                  "size-2 rounded-full",
                  live ? "bg-dot-ok shadow-[0_0_0_3px_rgba(31,138,76,0.2)]" : "bg-dot-off",
                )}
              />
              {live ? "Live · 2 s" : "Live off"}
            </button>
            <Button variant="line" size="bar" disabled={!active || q.isFetching} onClick={refresh}>
              <RefreshCw />
              Refresh
            </Button>
          </>
        }
      />

      {tabsQ.isError && (
        <ErrorAlert className="items-center" onRetry={() => tabsQ.refetch()}>
          Failed to load log sources: {(tabsQ.error as Error).message}
        </ErrorAlert>
      )}
      {tabsQ.isLoading && <Skeleton className="h-96 w-full rounded-[14px]" />}

      {active && (
        <>
          <div className="-mt-1 flex flex-col gap-2.5">
            <div role="tablist" aria-label="Source" className="flex flex-wrap gap-1 self-start rounded-[10px] bg-soft p-1">
              {tabs.map((t) => (
                <button
                  key={t.id}
                  type="button"
                  role="tab"
                  aria-selected={t.id === active.id}
                  onClick={() => setParam("source", t.id)}
                  className={cn(
                    "min-h-8 rounded-[7px] px-3 text-[13px]",
                    t.id === active.id ? "bg-ink font-medium text-on-ink" : "text-ink hover:bg-surface/70",
                  )}
                >
                  {t.label}
                </button>
              ))}
            </div>
            <div className="flex flex-wrap items-center gap-1.5 text-xs text-ink-3">
              <Server className="h-3.5 w-3.5" />
              <span className="rounded-md bg-soft px-2 py-px font-mono text-ink">{active.container}</span>
              {active.kind === "filter" ? (
                <>
                  <span className="ml-1.5">matching</span>
                  {active.needles?.map((n) => (
                    <span key={n} className="rounded-full border border-field bg-surface px-2 py-px font-mono text-ink">
                      {n}
                    </span>
                  ))}
                </>
              ) : (
                <span className="ml-1.5">stdout + stderr</span>
              )}
            </div>
          </div>

          <LogsViewer
            key={active.id}
            payload={q.data}
            error={q.isError ? (q.error as Error).message : null}
            loading={q.isLoading}
            live={live}
            tail={tail}
            onTail={(n) => setParam("tail", String(n))}
            onRetry={() => q.refetch()}
          />
        </>
      )}
    </>
  );
}

function LogsViewer({
  payload,
  error,
  loading,
  live,
  tail,
  onTail,
  onRetry,
}: {
  payload: LogsPayload | undefined;
  error: string | null;
  loading: boolean;
  live: boolean;
  tail: number;
  onTail: (n: number) => void;
  onRetry: () => void;
}) {
  const [query, setQuery] = React.useState("");
  const [range, setRange] = React.useState(0);
  const [open, setOpen] = React.useState<string | null>(null);
  const tailLabel = React.useId();

  const lines = payload?.lines;
  const rows = React.useMemo<Row[]>(() => {
    // Identical lines in the same millisecond still need their own keys.
    const seen = new Map<string, number>();
    return (lines ?? []).map((l) => {
      const base = `${l.ts}|${l.raw}`;
      const n = seen.get(base) ?? 0;
      seen.set(base, n + 1);
      return { ...l, key: `${base}|${n}`, ms: l.ts ? Date.parse(l.ts) : null };
    });
  }, [lines]);
  const newest = rows.reduce((m, r) => (r.ms !== null && r.ms > m ? r.ms : m), -Infinity);
  // The previous poll's newest line: anything after it is new in this poll.
  const before = React.useRef<number | null>(null);
  const freshAfter = React.useMemo(() => before.current, [rows]);
  React.useEffect(() => {
    before.current = Number.isFinite(newest) ? newest : null;
  }, [newest]);

  const inRange = range
    ? rows.filter((r) => r.ms !== null && newest - r.ms <= range * 60_000)
    : rows;

  const toggle = (r: Row) => setOpen((o) => (o === r.key ? null : r.key));
  const columns: Column<Row>[] = [
    {
      key: "open",
      label: "",
      className: "w-10 py-1 pl-4 pr-0 first:pl-4 phone:first:pl-3",
      cell: (r) => (
        <button
          type="button"
          aria-label={open === r.key ? "Hide line details" : "Show line details"}
          aria-expanded={open === r.key}
          onClick={() => toggle(r)}
          className="flex size-7 items-center justify-center rounded-md text-ink-3 hover:bg-soft"
        >
          {open === r.key ? <ChevronDown className="h-3.5 w-3.5" /> : <ChevronRight className="h-3.5 w-3.5" />}
        </button>
      ),
    },
    {
      key: "time",
      label: "Time",
      sortValue: (r) => r.ms ?? 0,
      className: "whitespace-nowrap py-1.5 font-mono text-xs tabular-nums text-ink-2 phone:px-1.5",
      cell: (r) => (r.ms === null ? "—" : <Time ms={r.ms} iso={r.ts!} />),
    },
    {
      key: "level",
      label: "Level",
      filterValue: (r) => (r.level ? LEVEL_LABEL[r.level] : null),
      filterOptions: LEVELS.map(([, label]) => label),
      className: "py-1.5 phone:px-1.5",
      cell: (r) =>
        r.level && (
          <span className={cn("inline-flex rounded-full px-[7px] py-px text-[11px] font-semibold", LEVEL_PILL[r.level])}>
            {LEVEL_LABEL[r.level]}
          </span>
        ),
    },
    {
      key: "logger",
      label: "Logger",
      filterValue: (r) => r.logger || null,
      hide: "phone",
      className: "whitespace-nowrap py-1.5 font-mono text-xs text-ink-3",
      cell: (r) => r.logger,
    },
    {
      key: "message",
      label: "Message",
      className: "w-[60%] max-w-0 py-1.5 font-mono text-xs",
      cell: (r) => (
        <button
          type="button"
          onClick={() => toggle(r)}
          className="block w-full truncate text-left text-ink phone:line-clamp-2 phone:whitespace-normal phone:break-all"
        >
          <span className="font-medium">{r.event}</span>
          {r.kv.length > 0 && (
            <span className="text-ink-3">{"  " + r.kv.map(([k, v]) => `${k}=${v}`).join(" ")}</span>
          )}
        </button>
      ),
    },
  ];
  const table = useTable({
    rows: inRange,
    columns,
    perPage: 25,
    initialSort: { key: "time", dir: "desc" },
    search: query.trim().toLowerCase(),
    match: (r, term) => r.raw.toLowerCase().includes(term),
  });
  const reset = () => {
    setQuery("");
    setRange(0);
    table.clearFilters();
  };
  const unavailable = error ?? (payload && !payload.ok ? payload.detail || "log unavailable" : null);

  return (
    <section aria-label="Log lines" className="min-w-0 rounded-[14px] border border-line bg-surface">
      <div className="flex flex-wrap items-center gap-2.5 border-b border-line px-6 py-3.5 phone:px-4">
        <label className="flex h-9 min-w-0 max-w-[360px] flex-[1_1_240px] items-center gap-2 rounded-[9px] border border-field px-3 text-ink-3 phone:max-w-full phone:flex-[1_1_100%]">
          <Search className="h-[15px] w-[15px] shrink-0" />
          <input
            type="search"
            aria-label="Filter lines"
            placeholder="Filter lines"
            value={query}
            onChange={(e) => setQuery(e.target.value)}
            className="min-w-0 flex-1 bg-transparent font-mono text-[13px] text-ink outline-none placeholder:text-ink-3"
          />
        </label>
        <LevelChips table={table} rows={inRange} />
        <div className="flex-1 phone:hidden" />
        <div role="group" aria-label="Time range" className="flex gap-0.5 rounded-[9px] bg-soft p-[3px]">
          {RANGES.map(([label, minutes]) => (
            <button
              key={label}
              type="button"
              aria-pressed={range === minutes}
              onClick={() => setRange(minutes)}
              className={segment(range === minutes)}
            >
              {label}
            </button>
          ))}
        </div>
        <div className="flex items-center gap-1.5 text-[13px] text-ink-2 phone:hidden">
          <span id={tailLabel}>Tail</span>
          <div role="group" aria-labelledby={tailLabel} className="flex gap-0.5 rounded-[9px] bg-soft p-[3px]">
            {TAILS.map((n) => (
              <button
                key={n}
                type="button"
                aria-pressed={tail === n}
                onClick={() => onTail(n)}
                className={segment(tail === n)}
              >
                {n >= 1000 ? `${n / 1000}k` : n}
              </button>
            ))}
          </div>
        </div>
      </div>

      {loading && <Skeleton className="m-6 h-80" />}

      {unavailable !== null && (
        <div role="alert" className="flex flex-col items-center gap-2.5 px-6 py-14 text-center">
          <div
            aria-hidden="true"
            className="flex size-11 items-center justify-center rounded-full bg-badge-danger text-badge-danger-fg"
          >
            <TriangleAlert className="h-5 w-5" />
          </div>
          <span className="text-[15px] font-semibold">Log unavailable</span>
          <span className="break-all font-mono text-xs text-ink-3">{unavailable}</span>
          <Button variant="line" size="bar" onClick={onRetry}>
            Retry
          </Button>
        </div>
      )}

      {payload?.ok && unavailable === null && (
        <>
          <div className="flex flex-wrap items-center gap-2 border-b border-divider px-6 py-2 text-xs tabular-nums text-ink-3 phone:px-4">
            <span>
              {rows.length} lines loaded · {table.total} in view ·{" "}
              {table.sort?.dir === "asc" ? "oldest first" : "newest first"}
            </span>
            {live && (
              <span className="inline-flex items-center gap-1.5 rounded-full bg-badge-success px-2 py-px font-semibold text-badge-success-fg">
                <span aria-hidden="true" className="size-1.5 rounded-full bg-dot-ok" />
                Live
              </span>
            )}
          </div>
          <DataTable
            table={table}
            rowKey={(r) => r.key}
            minWidth={860}
            rowClassName={(r) =>
              live && freshAfter !== null && r.ms !== null && r.ms > freshAfter
                ? "bg-badge-success/60"
                : open === r.key
                  ? "bg-row-hover"
                  : undefined
            }
            detail={(r) => (open === r.key ? <LineDetail line={r} /> : null)}
            empty={
              <EmptyState
                icon={FileText}
                title="No lines match"
                action={
                  <Button variant="line" size="bar" onClick={reset}>
                    Clear filters
                  </Button>
                }
              />
            }
          />
        </>
      )}
    </section>
  );
}

function LevelChips({ table, rows }: { table: TableModel<Row>; rows: Row[] }) {
  const chosen = table.filters.level ?? [];
  return (
    <div role="group" aria-label="Level" className="flex flex-wrap gap-1">
      {LEVELS.map(([level, label]) => {
        const on = chosen.includes(label);
        return (
          <button
            key={level}
            type="button"
            aria-pressed={on}
            onClick={() =>
              table.setFilter("level", on ? chosen.filter((v) => v !== label) : [...chosen, label])
            }
            className={cn(
              "inline-flex min-h-8 items-center gap-1.5 rounded-full border px-2.5 text-xs",
              on ? "border-ink bg-ink text-on-ink" : "border-field bg-surface text-ink hover:bg-row-hover",
            )}
          >
            <span aria-hidden="true" className={cn("size-2 rounded-full", LEVEL_DOT[level])} />
            {label}
            <span className={cn("tabular-nums", on ? "text-on-ink/70" : "text-ink-3")}>
              {rows.filter((r) => r.level === level).length}
            </span>
          </button>
        );
      })}
    </div>
  );
}

function Time({ ms, iso }: { ms: number; iso: string }) {
  const d = new Date(ms);
  return (
    <span title={iso}>
      {d.toLocaleTimeString("en-GB", { hour12: false })}
      <span className="text-side-muted phone:hidden">.{String(d.getMilliseconds()).padStart(3, "0")}</span>
    </span>
  );
}

function LineDetail({ line }: { line: Row }) {
  return (
    <div className="flex flex-col gap-2.5">
      <pre className="m-0 whitespace-pre-wrap break-all rounded-[10px] bg-side px-3.5 py-3 font-mono text-xs leading-relaxed text-[#E3E5E9]">
        {line.raw}
      </pre>
      <div className="flex flex-wrap items-start gap-3">
        {line.kv.length > 0 && (
          <dl className="m-0 grid min-w-0 flex-[1_1_420px] grid-cols-[max-content_minmax(0,1fr)] gap-x-4 gap-y-1 font-mono text-xs phone:flex-[1_1_100%]">
            {line.kv.map(([k, v], i) => (
              <React.Fragment key={i}>
                <dt className="text-ink-3">{k}</dt>
                <dd className="m-0 break-all text-ink">{v}</dd>
              </React.Fragment>
            ))}
          </dl>
        )}
        <Button
          variant="line"
          className="h-8 gap-1.5 rounded-lg px-3 text-[13px] font-normal [&_svg]:size-3.5"
          onClick={() => copyText(line.raw, "Copied line")}
        >
          <Copy />
          Copy line
        </Button>
      </div>
    </div>
  );
}
