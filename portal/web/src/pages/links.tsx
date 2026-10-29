// Links: the redirects the API serves at /links/<name>. The backend
// validates and audits every change; this page only edits and shows them.

import { useRef, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { toast } from "sonner";
import { Link2, Pencil, Play, Plus, Search } from "lucide-react";
import { api, ApiError } from "@/lib/api";
import { cn } from "@/lib/cn";
import { monthDay, monthDayTime } from "@/lib/format";
import { useEnv } from "@/hooks/use-env";
import { Button } from "@/components/ui/button";
import { Skeleton } from "@/components/ui/skeleton";
import { DataTable, useTable, type Column } from "@/components/data-table";
import {
  Dot,
  EmptyState,
  ErrorAlert,
  PageHeading,
  Panel,
  Pill,
  Switch,
  type DotTone,
  type Tone,
} from "@/components/page";
import type { LinkRow, LinkTestResult } from "@/types/api";
import { asMessage } from "./lists/format";
import { LinkDialog, type LinkBody } from "./links/LinkDialog";

type Check = "OK" | "Failing" | "Not checked" | "Disabled";
const CHECKS: Check[] = ["OK", "Failing", "Not checked", "Disabled"];
const CHECK_TONE: Record<Check, [Tone, DotTone]> = {
  OK: ["success", "ok"],
  Failing: ["danger", "bad"],
  "Not checked": ["neutral", "off"],
  Disabled: ["neutral", "off"],
};

function checkOf(link: LinkRow): Check {
  if (!link.enabled) return "Disabled";
  if (link.last_check_ok === null) return "Not checked";
  return link.last_check_ok ? "OK" : "Failing";
}

function every(minutes: number): string {
  if (minutes % 1440 === 0) return `${minutes / 1440} d`;
  if (minutes % 60 === 0) return `${minutes / 60} h`;
  return `${minutes} min`;
}

// FastAPI answers a body it cannot validate with a list of errors.
function errorText(e: unknown): string {
  const detail =
    e instanceof ApiError && e.body && typeof e.body === "object"
      ? (e.body as { detail?: unknown }).detail
      : undefined;
  if (Array.isArray(detail)) {
    return detail
      .map((d: { loc?: unknown[]; msg?: string }) =>
        [d.loc?.slice(1).join("."), d.msg].filter(Boolean).join(": "),
      )
      .join("; ");
  }
  return asMessage(e);
}

export function LinksPage() {
  const qc = useQueryClient();
  const env = useEnv();
  // null: closed. `link` is null for a new one; `key` tells editors apart.
  const [editor, setEditor] = useState<{ key: number; link: LinkRow | null } | null>(null);
  const editorKey = useRef(0);
  editorKey.current = editor?.key ?? 0;
  const openedEditors = useRef(0);
  const [query, setQuery] = useState("");

  const linksQ = useQuery<{ links: LinkRow[] }>({
    queryKey: ["links"],
    queryFn: () => api<{ links: LinkRow[] }>("/api/links"),
    // Scheduled checks change the rows while the page is open.
    refetchInterval: 15_000,
  });
  const refresh = () => qc.invalidateQueries({ queryKey: ["links"] });

  const saveMut = useMutation({
    mutationFn: ({ target, body }: { key: number; target: string; body: LinkBody }) => {
      const { name, ...fields } = body;
      return target
        ? api<LinkRow>(`/api/links/${target}`, { method: "PATCH", json: fields })
        : api<LinkRow>("/api/links", { method: "POST", json: { name, ...fields } });
    },
    onSuccess: (link, { key, target }) => {
      toast.success(`${target ? "Saved" : "Created"} ${link.name}`);
      // Another editor opened meanwhile stays open.
      setEditor((current) => (current?.key === key ? null : current));
      refresh();
    },
    // The open editor shows its own error; one closed meanwhile gets a toast.
    onError: (e, { key }) => {
      if (editorKey.current !== key) toast.error(errorText(e));
    },
  });

  const toggleMut = useMutation({
    mutationFn: (link: LinkRow) =>
      api<LinkRow>(`/api/links/${link.name}`, {
        method: "PATCH",
        json: { enabled: !link.enabled },
      }),
    onSuccess: (link) => {
      toast.success(`${link.enabled ? "Enabled" : "Disabled"} ${link.name}`);
      refresh();
    },
    onError: (e) => toast.error(errorText(e)),
  });

  const testMut = useMutation({
    mutationFn: (name: string) =>
      api<LinkTestResult>(`/api/links/${name}/test`, { method: "POST" }),
    onSuccess: (result, name) => {
      (result.ok ? toast.success : toast.error)(`${name}: ${result.detail}`);
      refresh();
    },
    onError: (e) => toast.error(errorText(e)),
  });

  const openEditor = (link: LinkRow | null) => {
    openedEditors.current += 1;
    setEditor({ key: openedEditors.current, link });
  };

  const columns: Column<LinkRow>[] = [
    {
      key: "name",
      label: "Name",
      sortValue: (l) => l.name,
      cell: (l) => (
        <div title={l.name} className="max-w-[240px] truncate tablet:max-w-[160px] phone:max-w-[140px]">
          {l.name}
        </div>
      ),
      className: "whitespace-nowrap font-mono text-[13px] font-medium",
    },
    {
      key: "target",
      label: "Target",
      sortValue: (l) => l.target_url,
      hide: "phone",
      // Takes the room the other columns leave, truncating the URL to fit.
      className: "w-full min-w-[160px] max-w-0 tablet:min-w-[120px]",
      cell: (l) => (
        <>
          <a
            href={l.target_url}
            target="_blank"
            rel="noreferrer noopener"
            title={l.target_url}
            className="block truncate text-[13px] text-link hover:text-link-hover"
          >
            {l.target_url}
          </a>
          <span className="text-xs text-ink-3">
            {l.expectations.length} {l.expectations.length === 1 ? "rule" : "rules"}
          </span>
        </>
      ),
    },
    {
      key: "check",
      label: "Check",
      sortValue: checkOf,
      filterValue: checkOf,
      filterOptions: CHECKS,
      className: "max-w-[200px] tablet:max-w-[180px]",
      cell: (l) => {
        const check = checkOf(l);
        return (
          <>
            <Pill tone={CHECK_TONE[check][0]} dot={CHECK_TONE[check][1]}>
              {check}
            </Pill>
            {l.enabled && l.last_check_detail && (
              <div
                title={l.last_check_detail}
                className="mt-1 truncate text-xs text-ink-3 phone:hidden"
              >
                {l.last_check_detail}
              </div>
            )}
          </>
        );
      },
    },
    {
      key: "checked",
      label: "Last checked",
      sortValue: (l) => l.last_check_at ?? "",
      hide: "phone",
      className: "whitespace-nowrap text-[13px] tabular-nums text-ink-2",
      cell: (l) => (l.last_check_at ? monthDayTime(l.last_check_at) : "—"),
    },
    {
      key: "interval",
      label: "Every",
      sortValue: (l) => l.check_interval_minutes,
      align: "right",
      hide: "tablet",
      className: "whitespace-nowrap text-[13px] tabular-nums",
      cell: (l) => every(l.check_interval_minutes),
    },
    {
      key: "updated",
      label: "Updated",
      sortValue: (l) => l.updated_at,
      hide: "tablet",
      className: "whitespace-nowrap text-[13px] text-ink-2",
      cell: (l) => (
        <>
          {monthDay(l.updated_at)}
          <div className="text-xs text-ink-3">{l.updated_by}</div>
        </>
      ),
    },
    {
      key: "actions",
      label: "",
      className: "py-2 last:pr-4",
      cell: (l) => (
        <div className="flex items-center justify-end gap-1 whitespace-nowrap">
          <Button
            variant="line"
            className="h-[34px] gap-1.5 rounded-lg px-3 text-[13px] font-normal [&_svg]:size-[13px]"
            disabled={testMut.isPending}
            onClick={() => testMut.mutate(l.name)}
          >
            <Play className="fill-current" strokeWidth={0} />
            {testMut.isPending && testMut.variables === l.name ? "Testing…" : "Test"}
          </Button>
          <Switch
            checked={l.enabled}
            label={`${l.enabled ? "Disable" : "Enable"} ${l.name}`}
            disabled={toggleMut.isPending}
            onCheckedChange={() => toggleMut.mutate(l)}
            className="mx-1"
          />
          <Button
            variant="quiet"
            size="iconSm"
            aria-label={`Edit ${l.name}`}
            title="Edit"
            onClick={() => openEditor(l)}
          >
            <Pencil />
          </Button>
        </div>
      ),
    },
  ];

  const links = linksQ.data?.links;
  const q = query.trim().toLowerCase();
  const table = useTable({
    rows: links ?? [],
    columns,
    initialSort: { key: "name", dir: "asc" },
    search: q,
    match: (l, term) =>
      l.name.toLowerCase().includes(term) || l.target_url.toLowerCase().includes(term),
  });
  const checkFilter = table.filters.check ?? [];
  const host = env.data?.backend_public_url.replace(/^https?:\/\//, "").replace(/\/+$/, "") ?? "";

  return (
    <>
      <PageHeading
        group="System"
        title="Links"
        meta={`${host}/links/<name>`}
        actions={
          <Button variant="ink" size="form" onClick={() => openEditor(null)}>
            <Plus />
            New link
          </Button>
        }
      />

      {links && links.length > 0 && (
        <div className="grid grid-cols-[repeat(auto-fit,minmax(180px,1fr))] gap-3 phone:grid-cols-2">
          {CHECKS.map((k) => {
            const on = checkFilter.length === 1 && checkFilter[0] === k;
            return (
              <button
                key={k}
                type="button"
                aria-pressed={on}
                onClick={() => table.setFilter("check", on ? [] : [k])}
                className={cn(
                  "flex flex-col gap-1 rounded-[14px] border-2 bg-surface px-4 py-3.5 text-left",
                  on ? "border-ink" : "border-line hover:border-field",
                )}
              >
                <span className="flex items-center gap-1.5 text-xs font-medium text-ink-3">
                  <Dot tone={CHECK_TONE[k][1]} />
                  {k}
                </span>
                <span className="text-2xl font-semibold tabular-nums">
                  {links.filter((l) => checkOf(l) === k).length}
                </span>
              </button>
            );
          })}
        </div>
      )}

      <Panel
        title="Redirects"
        aside={
          <label className="flex h-9 w-[280px] max-w-full items-center gap-2 rounded-[9px] border border-field px-3 text-ink-3">
            <Search className="h-[15px] w-[15px] shrink-0" />
            <input
              type="search"
              aria-label="Search links"
              placeholder="Name or target"
              value={query}
              onChange={(e) => setQuery(e.target.value)}
              className="min-w-0 flex-1 bg-transparent text-ink outline-none placeholder:text-ink-3"
            />
          </label>
        }
      >
        {linksQ.isLoading && <Skeleton className="m-6 h-40" />}
        {linksQ.isError && (
          <ErrorAlert className="m-6 items-center" onRetry={() => linksQ.refetch()}>
            Failed to load links: {errorText(linksQ.error)}
          </ErrorAlert>
        )}
        {links && links.length === 0 && (
          <EmptyState
            icon={Link2}
            title="No links yet"
            action={
              <Button variant="ink" size="form" onClick={() => openEditor(null)}>
                New link
              </Button>
            }
          />
        )}
        {links && links.length > 0 && (
          <DataTable
            table={table}
            rowKey={(l) => l.name}
            minWidth={980}
            empty="No links match"
          />
        )}
      </Panel>

      {editor && (
        <LinkDialog
          key={editor.key}
          link={editor.link}
          pending={saveMut.isPending}
          error={
            saveMut.isError && saveMut.variables?.key === editor.key
              ? errorText(saveMut.error)
              : null
          }
          onClose={() => setEditor(null)}
          onSave={(body) =>
            saveMut.mutate({ key: editor.key, target: editor.link?.name ?? "", body })
          }
        />
      )}
    </>
  );
}
