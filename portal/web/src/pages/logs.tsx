import { useEffect, useLayoutEffect, useMemo, useRef, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { useSearchParams } from "react-router-dom";
import { RefreshCw } from "lucide-react";
import { api } from "@/lib/api";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Checkbox } from "@/components/ui/checkbox";
import { Tabs, TabsList, TabsTrigger } from "@/components/ui/tabs";
import { PageHeader } from "@/components/ui/section";
import type { LogsPayload, LogsTab } from "@/types/api";

type TabsPayload = {
  tabs: LogsTab[];
  default_tail: number;
  max_tail: number;
};

const POLL_MS = 2000;
const LIVE_STORAGE = "tigerduck.portal.logs.live";

export function LogsPage() {
  const tabsQ = useQuery<TabsPayload>({
    queryKey: ["logs-tabs"],
    queryFn: () => api<TabsPayload>("/api/logs/tabs"),
    staleTime: Infinity,
  });

  const [params, setParams] = useSearchParams();
  const tabs = tabsQ.data?.tabs ?? [];
  const defaultTail = tabsQ.data?.default_tail ?? 500;
  const maxTail = tabsQ.data?.max_tail ?? 5000;

  const source = params.get("source") || "backend";
  const tail = Math.max(
    1,
    Math.min(maxTail, Number(params.get("tail")) || defaultTail),
  );

  const active = useMemo(
    () => tabs.find((t) => t.id === source) ?? tabs[0],
    [tabs, source],
  );

  return (
    <div className="space-y-6">
      <PageHeader
        title="Logs"
        description={active ? <ActiveDescription tab={active} /> : null}
      />

      {tabs.length > 0 && (
        <Tabs
          value={source}
          onValueChange={(v) => {
            const next = new URLSearchParams(params);
            next.set("source", v);
            setParams(next, { replace: true });
          }}
        >
          <TabsList className="flex w-full justify-start overflow-x-auto">
            {tabs.map((t) => (
              <TabsTrigger key={t.id} value={t.id} className="shrink-0">
                {t.label}
              </TabsTrigger>
            ))}
          </TabsList>
        </Tabs>
      )}

      {active && (
        <LogsViewer
          tab={active}
          tail={tail}
          maxTail={maxTail}
          onTailChange={(v) => {
            const next = new URLSearchParams(params);
            next.set("tail", String(v));
            setParams(next, { replace: true });
          }}
        />
      )}
    </div>
  );
}

function ActiveDescription({ tab }: { tab: LogsTab }) {
  if (tab.kind === "raw") {
    return (
      <>
        Container <code>{tab.container}</code> — raw stdout + stderr.
      </>
    );
  }
  return (
    <>
      Backend lines matching:{" "}
      {tab.needles?.map((n, i) => (
        <span key={n}>
          <code>{n}</code>
          {i < (tab.needles?.length ?? 0) - 1 ? ", " : ""}
        </span>
      ))}
    </>
  );
}

function LogsViewer({
  tab,
  tail,
  maxTail,
  onTailChange,
}: {
  tab: LogsTab;
  tail: number;
  maxTail: number;
  onTailChange: (v: number) => void;
}) {
  const [live, setLive] = useState<boolean>(() => {
    try {
      return localStorage.getItem(LIVE_STORAGE) === "1";
    } catch {
      return false;
    }
  });
  const [search, setSearch] = useState("");
  const [tailDraft, setTailDraft] = useState(tail);

  useEffect(() => setTailDraft(tail), [tail]);

  const q = useQuery<LogsPayload>({
    queryKey: ["logs-data", tab.id, tail],
    queryFn: () =>
      api<LogsPayload>(
        `/api/logs/data?source=${encodeURIComponent(tab.id)}&tail=${tail}`,
      ),
    refetchInterval: live ? POLL_MS : false,
    refetchIntervalInBackground: false,
  });

  useEffect(() => {
    try {
      localStorage.setItem(LIVE_STORAGE, live ? "1" : "0");
    } catch {
      // localStorage unavailable (private mode, locked-down browser); the
      // preference just won't survive a reload, which is acceptable.
    }
  }, [live]);

  const text = q.data?.text ?? "";
  const allLines = useMemo(() => text.split("\n"), [text]);
  const visibleLines = useMemo(() => {
    const needle = search.trim().toLowerCase();
    if (!needle) return allLines;
    return allLines.filter((l) => l.toLowerCase().includes(needle));
  }, [allLines, search]);

  const preRef = useRef<HTMLPreElement>(null);
  const wasAtBottom = useRef(true);

  useLayoutEffect(() => {
    const el = preRef.current;
    if (!el) return;
    // Capture pre-update position so the auto-follow check uses the
    // last-rendered scroll geometry, not the new one.
    if (wasAtBottom.current) {
      el.scrollTop = el.scrollHeight;
    }
  }, [visibleLines]);

  function onScroll(e: React.UIEvent<HTMLPreElement>) {
    const el = e.currentTarget;
    wasAtBottom.current =
      el.scrollHeight - el.scrollTop - el.clientHeight < 24;
  }

  return (
    <div className="space-y-3">
      <div className="flex flex-wrap items-end gap-3">
        <div className="grid gap-1.5">
          <Label htmlFor="tail">Tail</Label>
          <div className="flex gap-1.5">
            <Input
              id="tail"
              type="number"
              min={1}
              max={maxTail}
              value={tailDraft}
              onChange={(e) => setTailDraft(Number(e.target.value) || 1)}
              className="w-28"
            />
            <Button
              variant="outline"
              size="sm"
              onClick={() => onTailChange(tailDraft)}
            >
              Reload
            </Button>
          </div>
        </div>
        <div className="grid flex-1 gap-1.5">
          <Label htmlFor="search">Filter</Label>
          <Input
            id="search"
            type="search"
            placeholder="filter lines…"
            value={search}
            onChange={(e) => setSearch(e.target.value)}
          />
        </div>
        <label className="flex h-9 items-center gap-2 text-sm">
          <Checkbox
            checked={live}
            onCheckedChange={(v) => setLive(v === true)}
          />
          Live ({POLL_MS / 1000}s)
        </label>
        <Button
          variant="outline"
          size="icon"
          onClick={() => q.refetch()}
          disabled={q.isFetching}
          title="Refresh once"
        >
          <RefreshCw
            className={q.isFetching ? "h-4 w-4 animate-spin" : "h-4 w-4"}
          />
        </Button>
      </div>

      {q.data && q.data.ok === false && (
        <div className="rounded-md border border-destructive/40 bg-destructive/10 px-3 py-2 text-sm text-destructive">
          {q.data.detail || "log unavailable"}
        </div>
      )}

      <div className="flex items-center justify-between text-xs text-muted-foreground">
        <span>
          {search
            ? `${visibleLines.length} / ${allLines.length} lines`
            : `${allLines.length} lines`}
        </span>
        {live && <Badge variant="success">live</Badge>}
      </div>

      <pre
        ref={preRef}
        onScroll={onScroll}
        className="max-h-[70vh] overflow-auto rounded-lg border border-border bg-card p-4 font-mono text-xs leading-relaxed text-foreground/90"
      >
        {visibleLines.length > 0 ? visibleLines.join("\n") : "(empty)"}
      </pre>
    </div>
  );
}
