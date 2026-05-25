import { useQuery } from "@tanstack/react-query";
import {
  CheckCircle2,
  Circle,
  ExternalLink,
  Loader2,
  XCircle,
} from "lucide-react";
import { Badge } from "@/components/ui/badge";
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
} from "@/components/ui/card";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";
import { PageHeader, Section } from "@/components/ui/section";
import { Skeleton } from "@/components/ui/skeleton";
import { api } from "@/lib/api";
import type { ApnsConfig, FcmConfig, StatusPayload } from "@/types/api";

export function StatusPage() {
  const q = useQuery<StatusPayload>({
    queryKey: ["status"],
    queryFn: () => api<StatusPayload>("/api/status"),
    refetchInterval: 15_000,
  });

  return (
    <div className="space-y-8">
      <PageHeader
        title="Stack status"
        description="Same fields ./start.sh prints when the stack comes up."
      />

      {q.isLoading && <StatusSkeleton />}
      {q.isError && (
        <Card>
          <CardContent className="text-sm text-destructive">
            Failed to load status: {(q.error as Error).message}
          </CardContent>
        </Card>
      )}
      {q.data && <StatusContent data={q.data} />}
    </div>
  );
}

function StatusContent({ data }: { data: StatusPayload }) {
  const {
    env,
    containers,
    postgres,
    llm,
    backend_version: version,
    secrets,
    fcm_config: fcm,
    apns_config: apns,
  } = data;

  // Backend mounts FastAPI under /v2 (or whatever api_base_path reports).
  // Display the rooted URL so the link lands directly on the API surface
  // teams paste into curl / Postman, not the 404 you get hitting the
  // bare host.
  const apiBase = version.api_base_path || "/v2";
  const backendApiUrl = `${env.backend_public_url.replace(/\/+$/, "")}${apiBase}`;

  return (
    <div className="space-y-8">
      <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-5">
        <SummaryCard
          label="Mode"
          value={env.env || "(unset)"}
          tone={env.env === "production" ? "warning" : "success"}
        />
        <SummaryCard
          label="APNs"
          value={apnsSummaryValue(apns)}
          tone={apnsSummaryTone(apns)}
          detail={apns.state === "ok" ? undefined : apns.detail}
        />
        <SummaryCard
          label="FCM"
          value={fcmSummaryValue(fcm)}
          tone={fcmSummaryTone(fcm)}
          detail={fcm.state === "ok" ? undefined : fcm.detail}
        />
        <SummaryCard
          label="Backend"
          value={version.ok ? version.version || "?" : "unreachable"}
          tone={version.ok ? "default" : "destructive"}
          detail={
            version.ok
              ? `api ${version.api_base_path ?? ""}`
              : version.detail
          }
        />
        <SummaryCard
          label="LLM probe"
          value={env.skip_llm_probe ? "skipped" : "engaged"}
          tone={env.skip_llm_probe ? "muted" : "default"}
        />
      </div>

      <Section
        title="Overview"
        description="Mode + LLM-probe toggles live in .env — edit and ./start.sh from a terminal."
      >
        <Card>
          <CardContent className="p-0">
            <dl className="divide-y divide-border">
              <Row label="Log level" value={<code>{env.log_level}</code>} />
              <Row
                label="Backend"
                value={
                  <ExternalLinkValue href={backendApiUrl}>
                    {backendApiUrl}
                  </ExternalLinkValue>
                }
              />
              <Row
                label="Portal"
                value={
                  <ExternalLinkValue href={env.portal_public_url}>
                    {env.portal_public_url}
                  </ExternalLinkValue>
                }
              />
              <Row
                label="LLM URL"
                value={<code>{env.llm_base_url || "(unset)"}</code>}
              />
              <Row
                label="LAN backend"
                value={
                  env.host_lan_ips.length ? (
                    <div className="flex flex-wrap gap-2">
                      {env.host_lan_ips.map((ip) => {
                        const api = `http://${ip}:40000${apiBase}`;
                        return (
                          <ExternalLinkValue key={ip} href={api}>
                            {api}
                          </ExternalLinkValue>
                        );
                      })}
                    </div>
                  ) : (
                    <span className="text-muted-foreground">
                      (no LAN IP threaded in — start.sh detects en*/eth*/wlan*)
                    </span>
                  )
                }
              />
              <Row
                label="LAN portal"
                value={
                  env.host_lan_ips.length ? (
                    <div className="flex flex-wrap gap-2">
                      {env.host_lan_ips.map((ip) => (
                        <ExternalLinkValue
                          key={ip}
                          href={`http://${ip}:40010`}
                        >
                          {`http://${ip}:40010`}
                        </ExternalLinkValue>
                      ))}
                    </div>
                  ) : (
                    <span className="text-muted-foreground">
                      (no LAN IP threaded in)
                    </span>
                  )
                }
              />
            </dl>
          </CardContent>
        </Card>
      </Section>

      <Section title="Containers">
        {containers.length === 0 ? (
          <Card>
            <CardContent className="text-sm text-muted-foreground">
              No containers visible — the docker socket may not be mounted,
              or none of the expected containers exist yet.
            </CardContent>
          </Card>
        ) : (
          <Table>
            <TableHeader>
              <TableRow>
                <TableHead>Name</TableHead>
                <TableHead>State</TableHead>
                <TableHead>Health</TableHead>
                <TableHead>Restarts</TableHead>
                <TableHead>Image</TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {containers.map((c) => (
                <TableRow key={c.name}>
                  <TableCell className="font-mono text-xs">{c.name}</TableCell>
                  <TableCell>
                    <ContainerState
                      state={c.state}
                      detail={c.detail}
                    />
                  </TableCell>
                  <TableCell className="text-sm">{c.health ?? "—"}</TableCell>
                  <TableCell className="text-sm tabular-nums">
                    {c.restart_count ?? "—"}
                  </TableCell>
                  <TableCell className="font-mono text-xs text-muted-foreground">
                    {c.image ?? "—"}
                  </TableCell>
                </TableRow>
              ))}
            </TableBody>
          </Table>
        )}
      </Section>

      <Section title="Postgres">
        <Card>
          <CardContent className="space-y-3">
            {postgres.reachable ? (
              <>
                <div className="flex items-center gap-2 text-sm">
                  <CheckCircle2 className="h-4 w-4 text-success" />
                  Reachable. Alembic head:{" "}
                  <code>{postgres.alembic_head || "?"}</code>
                </div>
                {postgres.rows && (
                  <Table>
                    <TableHeader>
                      <TableRow>
                        <TableHead>Table</TableHead>
                        <TableHead>Rows</TableHead>
                      </TableRow>
                    </TableHeader>
                    <TableBody>
                      {Object.entries(postgres.rows).map(([table, count]) => (
                        <TableRow key={table}>
                          <TableCell className="font-mono text-xs">
                            {table}
                          </TableCell>
                          <TableCell className="tabular-nums">
                            {count}
                          </TableCell>
                        </TableRow>
                      ))}
                    </TableBody>
                  </Table>
                )}
              </>
            ) : (
              <div className="flex items-center gap-2 text-sm text-destructive">
                <XCircle className="h-4 w-4" />
                Not reachable — {postgres.detail}
              </div>
            )}
          </CardContent>
        </Card>
      </Section>

      <Section title="LLM">
        <Card>
          <CardContent className="space-y-2 text-sm">
            <div className="font-mono text-xs text-muted-foreground">
              {env.llm_base_url || "(unset)"}
            </div>
            <div>
              {llm.reachable ? (
                <span className="inline-flex items-center gap-1 text-success">
                  <CheckCircle2 className="h-4 w-4" /> reachable (
                  {llm.status_code}, {llm.latency_ms}ms)
                </span>
              ) : (
                <span className="inline-flex items-center gap-1 text-destructive">
                  <XCircle className="h-4 w-4" /> not reachable — {llm.detail}
                </span>
              )}
            </div>
            {env.skip_llm_probe && (
              <div className="text-xs text-muted-foreground">
                Probe is skipped at startup, so this doesn't block boot.
              </div>
            )}
          </CardContent>
        </Card>
      </Section>

      <Section title="Secrets on disk">
        <Table>
          <TableHeader>
            <TableRow>
              <TableHead>File</TableHead>
              <TableHead>Path</TableHead>
              <TableHead>Present?</TableHead>
              <TableHead>Size</TableHead>
            </TableRow>
          </TableHeader>
          <TableBody>
            <SecretRow label="APNs key" data={secrets.apns_key} />
            <SecretRow label="FCM credentials" data={secrets.fcm_credentials} />
          </TableBody>
        </Table>
      </Section>
    </div>
  );
}

function Row({
  label,
  value,
}: {
  label: React.ReactNode;
  value: React.ReactNode;
}) {
  return (
    <div className="grid grid-cols-1 gap-1 px-6 py-3 sm:grid-cols-[180px_1fr] sm:items-center sm:gap-4">
      <dt className="text-xs font-medium uppercase tracking-wide text-muted-foreground">
        {label}
      </dt>
      <dd className="text-sm">{value}</dd>
    </div>
  );
}

function SummaryCard({
  label,
  value,
  tone = "default",
  detail,
}: {
  label: string;
  value: string;
  tone?: "default" | "success" | "warning" | "destructive" | "muted";
  detail?: string;
}) {
  return (
    <Card>
      <CardHeader className="pb-2">
        <CardDescription className="text-xs uppercase tracking-wide">
          {label}
        </CardDescription>
      </CardHeader>
      <CardContent className="space-y-1 pt-0">
        <Badge variant={tone}>{value}</Badge>
        {detail ? (
          <div className="truncate text-xs text-muted-foreground">{detail}</div>
        ) : null}
      </CardContent>
    </Card>
  );
}

function ContainerState({
  state,
  detail,
}: {
  state: string;
  detail?: string;
}) {
  const isUp = state === "running";
  const isBroken = state === "unreachable" || state === "error";
  const Icon = isUp ? CheckCircle2 : isBroken ? XCircle : Circle;
  return (
    <div className="flex items-center gap-2">
      <Icon
        className={
          isUp
            ? "h-4 w-4 text-success"
            : isBroken
              ? "h-4 w-4 text-destructive"
              : "h-4 w-4 text-muted-foreground"
        }
      />
      <span className="text-sm">{state}</span>
      {detail ? (
        <span className="text-xs text-muted-foreground">({detail})</span>
      ) : null}
    </div>
  );
}

function ExternalLinkValue({
  href,
  children,
}: {
  href: string;
  children: React.ReactNode;
}) {
  return (
    <a
      href={href}
      target="_blank"
      rel="noopener noreferrer"
      className="inline-flex items-center gap-1 font-mono text-xs text-primary hover:underline"
    >
      {children}
      <ExternalLink className="h-3 w-3" />
    </a>
  );
}

function SecretRow({
  label,
  data,
}: {
  label: string;
  data: { present: boolean; path: string; size_bytes?: number };
}) {
  return (
    <TableRow>
      <TableCell className="text-sm">{label}</TableCell>
      <TableCell className="font-mono text-xs text-muted-foreground">
        {data.path}
      </TableCell>
      <TableCell>
        {data.present ? (
          <Badge variant="success">yes</Badge>
        ) : (
          <Badge variant="destructive">no</Badge>
        )}
      </TableCell>
      <TableCell className="tabular-nums text-sm">
        {data.present ? data.size_bytes ?? "—" : "—"}
      </TableCell>
    </TableRow>
  );
}

// FCM badge text reflects the cross-check between TIGERDUCK_FCM_PROJECT_ID
// and the service-account JSON's project_id. The status helpers in
// portal/app/status.py are the source of truth for which state applies.
function fcmSummaryValue(fcm: FcmConfig): string {
  if (fcm.state === "ok") return fcm.project_id || "?";
  if (fcm.state === "mismatch") return "mismatch";
  if (fcm.state === "missing") return "disabled";
  return "disabled";
}

function fcmSummaryTone(
  fcm: FcmConfig,
): "default" | "success" | "warning" | "destructive" | "muted" {
  if (fcm.state === "ok") return "default";
  if (fcm.state === "mismatch") return "warning";
  return "muted";
}

// APNs has no "mismatch" — there's nothing to compare against the .p8
// private key — so the badge collapses to env (dev/prod) when ok, or to
// "disabled" when anything is missing.
function apnsSummaryValue(apns: ApnsConfig): string {
  if (apns.state === "ok") return apns.apns_env || "?";
  return "disabled";
}

function apnsSummaryTone(
  apns: ApnsConfig,
): "default" | "success" | "warning" | "destructive" | "muted" {
  if (apns.state !== "ok") return "muted";
  return apns.apns_env === "production" ? "warning" : "success";
}

function StatusSkeleton() {
  return (
    <div className="space-y-6">
      <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-5">
        {[0, 1, 2, 3, 4].map((i) => (
          <Skeleton key={i} className="h-24" />
        ))}
      </div>
      <Skeleton className="h-48" />
      <Skeleton className="h-32" />
      <div className="flex items-center gap-2 text-sm text-muted-foreground">
        <Loader2 className="h-4 w-4 animate-spin" /> Loading…
      </div>
    </div>
  );
}
