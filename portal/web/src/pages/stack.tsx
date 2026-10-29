// Stack: the containers, database, LLM, secrets and endpoints behind the
// backend, from the same /api/status payload the Status page reads.

import { useQuery } from "@tanstack/react-query";
import { toast } from "sonner";
import { Check, ExternalLink, RefreshCw, Server, X } from "lucide-react";
import { api } from "@/lib/api";
import { fileSize } from "@/lib/format";
import { Button } from "@/components/ui/button";
import { Skeleton } from "@/components/ui/skeleton";
import { DataTable, useTable, type Column } from "@/components/data-table";
import { Dot, EmptyState, ErrorAlert, PageHeading, Panel, Pill, type DotTone } from "@/components/page";
import type { ContainerInfo, FilePresence, StatusPayload } from "@/types/api";

const STATE_TONE: Record<string, DotTone> = {
  running: "ok",
  restarting: "warn",
  unreachable: "bad",
  error: "bad",
};

// Docker reports StartedAt with nanoseconds, which Date.parse may not take.
function startedMs(c: ContainerInfo): number | null {
  if (c.state !== "running" || !c.started_at) return null;
  const ms = Date.parse(c.started_at.replace(/(\.\d{3})\d+/, "$1"));
  return Number.isNaN(ms) ? null : ms;
}

function uptime(ms: number): string {
  const minutes = Math.max(0, Math.floor((Date.now() - ms) / 60_000));
  const d = Math.floor(minutes / 1440);
  const h = Math.floor((minutes % 1440) / 60);
  if (d) return `${d}d ${h}h`;
  if (h) return `${h}h`;
  return `${minutes}m`;
}

function capitalize(s: string): string {
  return s.charAt(0).toUpperCase() + s.slice(1);
}

export function StackPage() {
  const q = useQuery<StatusPayload>({
    queryKey: ["status"],
    queryFn: () => api<StatusPayload>("/api/status"),
    refetchInterval: 15_000,
  });

  const refresh = () =>
    q.refetch().then((r) => {
      if (r.isSuccess) toast.success("Status refreshed");
    });

  return (
    <>
      <PageHeading
        group="System"
        title="Stack"
        actions={
          <>
            {q.dataUpdatedAt > 0 && (
              <span className="text-[13px] tabular-nums text-ink-3">
                Updated {new Date(q.dataUpdatedAt).toLocaleTimeString("en-GB")} · every 15 s
              </span>
            )}
            <Button variant="line" size="bar" disabled={q.isFetching} onClick={refresh}>
              <RefreshCw />
              Refresh
            </Button>
          </>
        }
      />
      {q.isLoading && <Skeleton className="h-96 w-full rounded-[14px]" />}
      {q.isError && (
        <ErrorAlert className="items-center" onRetry={() => q.refetch()}>
          Failed to load status: {(q.error as Error).message}
        </ErrorAlert>
      )}
      {q.data && <StackContent data={q.data} />}
    </>
  );
}

function StackContent({ data }: { data: StatusPayload }) {
  const { env, containers, postgres: pg, llm, backend_version: version, secrets } = data;
  const { fcm_config: fcm, apns_config: apns } = data;
  const apiBase = version.api_base_path || "/v3";
  const backendApiUrl = `${env.backend_public_url.replace(/\/+$/, "")}${apiBase}`;

  // Same values as the Status page's summary cards; non-ok notes are the server's detail.
  const tiles: { label: string; value: string; tone: DotTone; note: string }[] = [
    {
      label: "Mode",
      value: env.env || "(unset)",
      tone: env.env === "production" ? "warn" : "ok",
      note: `${capitalize(env.env || "unknown")} stack`,
    },
    {
      label: "APNs",
      value: apns.state === "ok" ? apns.apns_env || "?" : "disabled",
      tone: apns.state === "ok" ? "ok" : "off",
      note: apns.state === "ok" ? "Team and key ids set, key file readable" : apns.detail ?? "",
    },
    {
      label: "FCM",
      value: fcm.state === "ok" ? fcm.project_id || "?" : fcm.state === "mismatch" ? "mismatch" : "disabled",
      tone: fcm.state === "ok" ? "ok" : fcm.state === "mismatch" ? "warn" : "off",
      note: fcm.state === "ok" ? "Env and JSON project ids agree" : fcm.detail ?? "",
    },
    {
      label: "Backend",
      value: version.ok ? version.version || "?" : "unreachable",
      tone: version.ok ? "ok" : "bad",
      note: version.ok ? `api ${version.api_base_path ?? ""}` : version.detail ?? "",
    },
    {
      label: "LLM probe",
      value: env.skip_llm_probe ? "skipped" : "engaged",
      tone: env.skip_llm_probe ? "off" : "ok",
      note: env.skip_llm_probe ? "Skipped at start" : "Checked at start",
    },
  ];

  const lanBackends = env.host_lan_ips.map((ip) => `http://${ip}:40000${apiBase}`);
  const endpoints: { label: string; values: string[]; link: boolean }[] = [
    { label: "Backend API", values: [backendApiUrl], link: true },
    { label: "Portal", values: [env.portal_public_url], link: true },
    { label: "LAN backend", values: lanBackends, link: true },
    { label: "LLM URL", values: [env.llm_base_url || "(unset)"], link: false },
    { label: "Log level", values: [env.log_level], link: false },
    { label: "APNs environment", values: [apns.state === "ok" ? apns.apns_env : "—"], link: false },
  ];

  const rows = pg.rows ? Object.entries(pg.rows).map(([name, n]) => [name, Number(n)] as const) : [];
  const max = Math.max(1, ...rows.map(([, n]) => n));

  return (
    <>
      <div className="grid grid-cols-[repeat(auto-fit,minmax(190px,1fr))] gap-3 phone:grid-cols-2">
        {tiles.map((t) => (
          <div key={t.label} className="flex min-w-0 flex-col gap-1.5 rounded-[14px] border border-line bg-surface px-[18px] py-4">
            <span className="text-xs font-medium text-ink-3">{t.label}</span>
            <span title={t.value} className="truncate text-xl font-semibold tracking-[-0.01em]">
              {t.value}
            </span>
            <span title={t.note} className="flex min-w-0 items-center gap-1.5 text-xs text-ink-2">
              <Dot tone={t.tone} />
              <span className="truncate">{t.note}</span>
            </span>
          </div>
        ))}
      </div>

      <ContainersPanel containers={containers} />

      <div className="flex flex-wrap items-start gap-6">
        <Panel
          title="Postgres"
          aside={<State ok={pg.reachable} />}
          className="flex-[1_1_320px] overflow-hidden"
        >
          {pg.reachable ? (
            <>
              <div className="flex justify-between gap-3 px-5 pb-1.5 pt-3.5 text-[13px]">
                <span className="text-ink-3">Alembic head</span>
                <span className="font-mono">{pg.alembic_head || "?"}</span>
              </div>
              <ul className="pb-3 pt-1">
                {rows.map(([name, n]) => (
                  <li key={name} className="flex flex-col gap-1.5 px-5 py-2">
                    <div className="flex justify-between gap-3 text-[13px]">
                      <span className="break-all font-mono">{name}</span>
                      <span className="tabular-nums text-ink-2">{n.toLocaleString("en-US")}</span>
                    </div>
                    <div aria-hidden="true" className="h-1.5 rounded-full bg-divider">
                      <div
                        className="h-1.5 rounded-full bg-chart-1"
                        style={{ width: `${Math.max(2, Math.round((n / max) * 100))}%` }}
                      />
                    </div>
                  </li>
                ))}
              </ul>
            </>
          ) : (
            <p className="break-words px-5 py-4 font-mono text-xs text-badge-danger-fg">{pg.detail}</p>
          )}
        </Panel>

        <Panel title="LLM" aside={<State ok={llm.reachable} />} className="flex-[1_1_320px] overflow-hidden">
          <div className="flex flex-col gap-3 px-5 py-4">
            <div className="flex flex-wrap gap-x-6 gap-y-4">
              <Figure label="Status" value={llm.status_code ? String(llm.status_code) : "—"} />
              <Figure label="Latency" value={llm.latency_ms !== undefined ? `${llm.latency_ms} ms` : "—"} />
              <Figure label="Probe at start" value={env.skip_llm_probe ? "skipped" : "on"} />
            </div>
            <span className="break-all font-mono text-xs text-ink-3">{llm.url || env.llm_base_url || "(unset)"}</span>
            {!llm.reachable && llm.detail && (
              <span className="break-words font-mono text-xs text-badge-danger-fg">{llm.detail}</span>
            )}
          </div>
        </Panel>

        <Panel title="Secrets on disk" className="flex-[1_1_320px] overflow-hidden">
          <ul>
            <SecretRow label="APNs key" file={secrets.apns_key} />
            <SecretRow label="FCM credentials" file={secrets.fcm_credentials} />
          </ul>
        </Panel>
      </div>

      <Panel title="Endpoints and settings" aside={<span className="text-xs text-ink-3">from .env</span>} className="overflow-hidden">
        <dl className="grid grid-cols-[repeat(auto-fit,minmax(260px,1fr))]">
          {endpoints.map((e) => (
            <div key={e.label} className="flex min-w-0 flex-col gap-1 border-b border-divider px-6 py-3.5 phone:px-4">
              <dt className="text-xs text-ink-3">{e.label}</dt>
              <dd className="flex min-w-0 flex-col gap-1">
                {e.values.length === 0 && <span className="font-mono text-[13px]">—</span>}
                {e.values.map((v) =>
                  e.link ? (
                    <a
                      key={v}
                      href={v}
                      target="_blank"
                      rel="noopener noreferrer"
                      title={v}
                      className="inline-flex min-w-0 items-center gap-1 font-mono text-[13px] text-link hover:text-link-hover"
                    >
                      <span className="truncate">{v}</span>
                      <ExternalLink className="h-3 w-3 shrink-0" />
                    </a>
                  ) : (
                    <span key={v} className="break-all font-mono text-[13px]">
                      {v}
                    </span>
                  ),
                )}
              </dd>
            </div>
          ))}
        </dl>
      </Panel>
    </>
  );
}

function ContainersPanel({ containers }: { containers: ContainerInfo[] }) {
  const columns: Column<ContainerInfo>[] = [
    {
      key: "name",
      label: "Name",
      sortValue: (c) => c.name,
      className: "py-[11px] font-mono text-[13px]",
      cell: (c) => c.name,
    },
    {
      key: "state",
      label: "State",
      sortValue: (c) => c.state,
      filterValue: (c) => c.state,
      className: "py-[11px]",
      cell: (c) => (
        <span title={c.detail || c.state} className="inline-flex items-center gap-1.5 whitespace-nowrap">
          <Dot tone={STATE_TONE[c.state] ?? "off"} />
          {c.state}
        </span>
      ),
    },
    {
      key: "health",
      label: "Health",
      sortValue: (c) => c.health ?? "",
      filterValue: (c) => c.health ?? "—",
      hide: "phone",
      className: "py-[11px] text-ink-2",
      cell: (c) => c.health ?? "—",
    },
    {
      key: "uptime",
      label: "Uptime",
      // Sorts as a duration: the earliest start is the longest uptime.
      sortValue: (c) => {
        const ms = startedMs(c);
        return ms === null ? -1 : Date.now() - ms;
      },
      className: "whitespace-nowrap py-[11px] tabular-nums text-ink-2",
      cell: (c) => {
        const ms = startedMs(c);
        return ms === null ? "—" : uptime(ms);
      },
    },
    {
      key: "restarts",
      label: "Restarts",
      sortValue: (c) => c.restart_count ?? -1,
      align: "right",
      hide: "phone",
      className: "py-[11px] tabular-nums",
      cell: (c) => c.restart_count ?? "—",
    },
    {
      key: "image",
      label: "Image",
      sortValue: (c) => c.image ?? "",
      hide: "tablet",
      className: "break-all py-[11px] font-mono text-xs text-ink-3",
      cell: (c) => c.image ?? "—",
    },
  ];
  const table = useTable({
    rows: containers,
    columns,
    perPage: 8,
    initialSort: { key: "name", dir: "asc" },
  });
  const running = containers.filter((c) => c.state === "running").length;

  return (
    <Panel
      title="Containers"
      aside={
        <span className="text-xs text-ink-3">
          {running} running · {containers.length - running} not running
        </span>
      }
    >
      {containers.length === 0 ? (
        <EmptyState icon={Server} title="No containers visible" />
      ) : (
        <DataTable
          table={table}
          rowKey={(c) => c.name}
          empty="No containers match these filters"
        />
      )}
    </Panel>
  );
}

function State({ ok }: { ok: boolean }) {
  return (
    <span className="flex items-center gap-1.5 text-[13px]">
      <Dot tone={ok ? "ok" : "bad"} />
      {ok ? "Reachable" : "Not reachable"}
    </span>
  );
}

function Figure({ label, value }: { label: string; value: string }) {
  return (
    <div className="flex flex-col gap-0.5">
      <span className="text-xs text-ink-3">{label}</span>
      <span className="text-xl font-semibold tabular-nums">{value}</span>
    </div>
  );
}

function SecretRow({ label, file }: { label: string; file: FilePresence }) {
  return (
    <li className="flex items-center justify-between gap-3 border-b border-divider px-5 py-3 last:border-b-0">
      <div className="flex min-w-0 flex-col gap-0.5">
        <span className="font-medium">{label}</span>
        <span className="break-all font-mono text-xs text-ink-3">{file.path}</span>
      </div>
      {file.present ? (
        <Pill tone="success">
          <Check className="h-3 w-3" />
          Present{file.size_bytes !== undefined ? ` · ${fileSize(file.size_bytes)}` : ""}
        </Pill>
      ) : (
        <Pill tone="danger">
          <X className="h-3 w-3" />
          Missing
        </Pill>
      )}
    </li>
  );
}
