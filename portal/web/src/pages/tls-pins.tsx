// TLS pins: the sets the apps' pin document is signed from, what the
// watcher last saw on each host, and every document published. Changes go
// through the backend, which holds the signing key and audits each one.

import { Fragment, useRef, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { toast } from "sonner";
import { ChevronRight, Copy, Plus, RefreshCw, Send, Trash2 } from "lucide-react";
import { api, ApiError } from "@/lib/api";
import { cn } from "@/lib/cn";
import { fullDate, monthDayTime } from "@/lib/format";
import { Button } from "@/components/ui/button";
import { Skeleton } from "@/components/ui/skeleton";
import { DataTable, useTable, type Column } from "@/components/data-table";
import {
  ErrorAlert,
  FormDialog,
  PageHeading,
  Panel,
  Pill,
  Switch,
  type DotTone,
  type Tone,
} from "@/components/page";
import { TlsPinDialog, type PinDialogKind, type PinDialogResult } from "./tls-pins/TlsPinDialog";

type Pin = {
  spki: string;
  label: string;
  first_seen_at: string | null;
  last_seen_at: string | null;
  source: string;
  pending?: boolean;
  stale: boolean;
};

type ChainCert = {
  subject: string;
  issuer: string;
  not_before: string;
  not_after: string;
  spki: string;
};

type WatchResult = {
  checked_at: string;
  status: "trusted" | "untrusted" | "error";
  detail: string | null;
  via_aia: boolean;
  chain: ChainCert[];
  added: string[];
};

type PinSet = {
  id: string;
  domains: { name: string; include_subdomains: boolean }[];
  pins: Pin[];
  expires_at: string;
  monitored_hosts: string[];
  auto_publish: boolean;
  watch_results: Record<string, WatchResult>;
  updated_at: string;
  updated_by: string | null;
};

type PinDocument = {
  serial: number;
  key_id: string;
  issued_at: string;
  expires_at: string;
  published_at: string;
  reason: string;
};

type TlsPinsResponse = { sets: PinSet[]; documents: PinDocument[] };

type ActionResult = { ok: boolean; serial: number | null };

type Action = {
  path: string;
  method: "POST" | "PUT";
  json?: unknown;
  publishes: boolean;
  dialog?: number;
};
type Mutate = (a: Action) => void;

// `key` tells dialogs apart, so a late answer closes only the one that sent it.
type Dialog = { key: number; kind: PinDialogKind; set: PinSet; pin?: Pin };

type HostRow = { host: string; result?: WatchResult };

type Confirm = { key: number; kind: "publish" | "resign" };

function day(iso: string | null): string {
  return iso ? iso.slice(0, 10) : "—";
}

function shortPin(spki: string): string {
  return `${spki.slice(0, 10)}…${spki.slice(-6)}`;
}

function daysFromNow(iso: string): number {
  return Math.round((Date.parse(iso) - Date.now()) / 86_400_000);
}

function commonName(subject: string): string {
  return /(?:^|,)CN=([^,]+)/.exec(subject)?.[1] ?? subject;
}

const setPath = (id: string, rest: string) => `/sets/${encodeURIComponent(id)}${rest}`;

// Publish now approves only the pending pins on screen, not ones found since.
const shownPending = (data?: TlsPinsResponse) =>
  Object.fromEntries(
    (data?.sets ?? []).map((s) => [s.id, s.pins.filter((p) => p.pending).map((p) => p.spki)]),
  );

type PinStatus = "Published" | "Not published" | "Unseen 90 days" | "Renewal root";
const PIN_STATUSES: PinStatus[] = ["Published", "Not published", "Unseen 90 days", "Renewal root"];
const PIN_TONE: Record<PinStatus, Tone> = {
  Published: "success",
  "Not published": "warning",
  "Unseen 90 days": "orange",
  "Renewal root": "neutral",
};

function statusOf(p: Pin): PinStatus {
  if (p.pending) return "Not published";
  if (p.stale) return "Unseen 90 days";
  // Never served by a monitored host: kept for the chain that replaces today's.
  if (p.last_seen_at === null) return "Renewal root";
  return "Published";
}

const RESULT_TONE: Record<WatchResult["status"], [Tone, DotTone]> = {
  trusted: ["success", "ok"],
  untrusted: ["danger", "bad"],
  error: ["warning", "warn"],
};

function reasonTone(reason: string): Tone {
  if (reason.includes("remove")) return "orange";
  if (reason.includes("expir")) return "violet";
  if (reason.includes("resign")) return "neutral";
  return "info";
}

function copyPin(spki: string) {
  navigator.clipboard.writeText(spki).then(
    () => toast.success("Copied pin"),
    () => toast.error("Copy failed"),
  );
}

export function TlsPinsPage() {
  const qc = useQueryClient();
  const [dialog, setDialog] = useState<Dialog | null>(null);
  const openedDialogs = useRef(0);
  const openDialog = (d: Omit<Dialog, "key">) => {
    openedDialogs.current += 1;
    setDialog({ ...d, key: openedDialogs.current });
  };
  const [confirm, setConfirm] = useState<Confirm | null>(null);
  const openConfirm = (kind: Confirm["kind"]) => {
    openedDialogs.current += 1;
    setConfirm({ kind, key: openedDialogs.current });
  };

  const pinsQ = useQuery<TlsPinsResponse>({
    queryKey: ["tls-pins"],
    queryFn: () => api<TlsPinsResponse>("/api/tls-pins"),
  });

  const action = useMutation({
    mutationFn: (a: Action) =>
      api<ActionResult>(`/api/tls-pins${a.path}`, {
        method: a.method,
        json: a.json,
      }).then((r) => ({ ...r, publishes: a.publishes })),
    onSuccess: (r, a) => {
      // Another dialog opened meanwhile stays open.
      setDialog((current) => (current?.key === a.dialog ? null : current));
      setConfirm((current) => (current?.key === a.dialog ? null : current));
      if (!r.publishes) toast.success("Saved");
      else if (r.serial !== null) toast.success(`Published serial ${r.serial}`);
      else toast.warning("Saved, not published: no signing key");
      qc.invalidateQueries({ queryKey: ["tls-pins"] });
    },
    onError: (e: unknown) =>
      toast.error(e instanceof ApiError ? e.message : "Request failed"),
  });

  const save = (d: Dialog, r: PinDialogResult) => {
    const id = d.set.id;
    const send = (a: Omit<Action, "dialog">) => action.mutate({ ...a, dialog: d.key });
    if (r.kind === "pin") {
      send({ path: setPath(id, "/pins"), method: "POST", json: { spki: r.spki, label: r.label }, publishes: true });
    } else if (r.kind === "hosts") {
      send({ path: setPath(id, "/monitored-hosts"), method: "PUT", json: { hosts: r.hosts }, publishes: false });
    } else if (r.kind === "expiry") {
      send({ path: setPath(id, "/expires-at"), method: "PUT", json: { expires_at: `${r.date}T00:00:00Z` }, publishes: true });
    } else {
      send({ path: setPath(id, "/pins/remove"), method: "POST", json: { spki: r.spki }, publishes: true });
    }
  };

  const data = pinsQ.data;
  const pendingPins = (data?.sets ?? []).flatMap((s) => s.pins.filter((p) => p.pending));
  const pendingCount = pendingPins.length;

  return (
    <>
      <PageHeading
        group="System"
        title="TLS pins"
        meta="/.well-known/tigerduck/tls-pins.json · watcher hourly"
        actions={
          <>
            <Button
              variant="line"
              size="form"
              disabled={action.isPending}
              onClick={() => openConfirm("resign")}
            >
              <RefreshCw />
              Re-sign now
            </Button>
            <Button
              variant="ink"
              size="form"
              disabled={action.isPending}
              onClick={() => openConfirm("publish")}
            >
              <Send />
              Publish now
              {pendingCount > 0 && (
                <span className="inline-flex h-5 min-w-5 items-center justify-center rounded-full bg-brand px-1.5 text-xs font-semibold text-side">
                  {pendingCount}
                </span>
              )}
            </Button>
          </>
        }
      />

      {pinsQ.isLoading && <Skeleton className="h-64 w-full rounded-[14px]" />}
      {pinsQ.isError && (
        <ErrorAlert className="items-center" onRetry={() => pinsQ.refetch()}>
          Failed to load TLS pins: {(pinsQ.error as Error).message}
        </ErrorAlert>
      )}
      {data?.sets.map((s) => (
        <PinSetSection
          key={s.id}
          set={s}
          busy={action.isPending}
          mutate={action.mutate}
          openDialog={openDialog}
        />
      ))}
      {data && <DocumentsPanel documents={data.documents} />}

      {dialog && (
        <TlsPinDialog
          key={dialog.key}
          kind={dialog.kind}
          setId={dialog.set.id}
          hosts={dialog.set.monitored_hosts}
          expires={day(dialog.set.expires_at)}
          pin={dialog.pin}
          pending={action.isPending}
          onClose={() => setDialog(null)}
          onSave={(r) => save(dialog, r)}
        />
      )}

      {confirm && (
        <FormDialog
          open
          onOpenChange={(open) => !open && setConfirm(null)}
          title={
            confirm.kind === "resign"
              ? "Re-sign the pin document?"
              : `Publish ${pendingCount} ${pendingCount === 1 ? "pin" : "pins"}?`
          }
          width={560}
          footer={
            <>
              <Button variant="line" size="form" onClick={() => setConfirm(null)}>
                Cancel
              </Button>
              <Button
                variant="ink"
                size="form"
                disabled={action.isPending}
                onClick={() =>
                  action.mutate(
                    confirm.kind === "resign"
                      ? { path: "/resign", method: "POST", publishes: true, dialog: confirm.key }
                      : {
                          path: "/publish",
                          method: "POST",
                          json: { approve: shownPending(data) },
                          publishes: true,
                          dialog: confirm.key,
                        },
                  )
                }
              >
                {confirm.kind === "resign" ? "Re-sign" : "Publish"}
              </Button>
            </>
          }
        >
          {confirm.kind === "publish" && pendingPins.length > 0 && (
            <ul className="flex flex-col divide-y divide-divider rounded-xl border border-line">
              {pendingPins.map((p) => (
                <li key={p.spki} className="flex flex-col gap-0.5 px-4 py-2.5">
                  <span className="break-words font-medium">{p.label}</span>
                  <span className="font-mono text-xs text-ink-2">{shortPin(p.spki)}</span>
                </li>
              ))}
            </ul>
          )}
        </FormDialog>
      )}
    </>
  );
}

function SubHeading({ title, aside, divided }: { title: string; aside: string; divided?: boolean }) {
  return (
    <div
      className={cn(
        "flex items-baseline justify-between gap-3 px-6 pb-1.5 phone:px-4",
        divided ? "border-t border-line pt-5" : "pt-3.5",
      )}
    >
      <h3 className="text-sm font-semibold">{title}</h3>
      <span className="text-xs text-ink-3">{aside}</span>
    </div>
  );
}

function PinSetSection({
  set: s,
  busy,
  mutate,
  openDialog,
}: {
  set: PinSet;
  busy: boolean;
  mutate: Mutate;
  openDialog: (d: Omit<Dialog, "key">) => void;
}) {
  const published = s.pins.filter((p) => !p.pending).length;
  const left = daysFromNow(s.expires_at);

  const pinColumns: Column<Pin>[] = [
    {
      key: "label",
      label: "Label",
      sortValue: (p) => p.label,
      className: "min-w-[180px] break-words font-medium",
      cell: (p) => p.label,
    },
    {
      key: "spki",
      label: "Pin",
      hide: "phone",
      cell: (p) => (
        <div className="flex items-center gap-1">
          <span title={p.spki} className="whitespace-nowrap font-mono text-xs text-ink-2">
            {shortPin(p.spki)}
          </span>
          <button
            type="button"
            aria-label="Copy pin"
            onClick={() => copyPin(p.spki)}
            className="flex size-7 items-center justify-center rounded-md text-ink-3 hover:bg-soft hover:text-ink"
          >
            <Copy className="h-3.5 w-3.5" />
          </button>
        </div>
      ),
    },
    {
      key: "status",
      label: "Status",
      sortValue: statusOf,
      filterValue: statusOf,
      filterOptions: PIN_STATUSES,
      cell: (p) => <Pill tone={PIN_TONE[statusOf(p)]}>{statusOf(p)}</Pill>,
    },
    {
      key: "source",
      label: "Source",
      sortValue: (p) => p.source,
      filterValue: (p) => p.source,
      hide: "phone",
      className: "text-ink-2",
      cell: (p) => p.source,
    },
    {
      key: "first",
      label: "First seen",
      sortValue: (p) => p.first_seen_at ?? "",
      hide: "tablet",
      className: "whitespace-nowrap tabular-nums text-ink-2",
      cell: (p) => day(p.first_seen_at),
    },
    {
      key: "last",
      label: "Last seen",
      sortValue: (p) => p.last_seen_at ?? "",
      hide: "phone",
      className: "whitespace-nowrap tabular-nums text-ink-2",
      cell: (p) => day(p.last_seen_at),
    },
    {
      key: "actions",
      label: "",
      className: "py-2 text-right last:pr-4",
      cell: (p) => {
        const lastOne = !p.pending && published === 1;
        return (
          <Button
            variant="quiet"
            size="iconSm"
            aria-label={`Remove ${p.label}`}
            title={lastOne ? "The last published pin stays" : "Remove"}
            disabled={busy || lastOne}
            onClick={() => openDialog({ kind: "remove", set: s, pin: p })}
          >
            <Trash2 />
          </Button>
        );
      },
    },
  ];
  const pins = useTable({
    rows: s.pins,
    columns: pinColumns,
    perPage: Infinity,
    initialSort: { key: "first", dir: "desc" },
  });

  const hostRows: HostRow[] = s.monitored_hosts.map((host) => ({ host, result: s.watch_results[host] }));
  const hostColumns: Column<HostRow>[] = [
    {
      key: "host",
      label: "Host",
      sortValue: (h) => h.host,
      className: "whitespace-nowrap font-mono text-[13px]",
      cell: (h) => h.host,
    },
    {
      key: "checked",
      label: "Checked",
      sortValue: (h) => h.result?.checked_at ?? "",
      hide: "phone",
      className: "whitespace-nowrap tabular-nums text-ink-2",
      cell: (h) => (h.result ? monthDayTime(h.result.checked_at) : "—"),
    },
    {
      key: "result",
      label: "Result",
      sortValue: (h) => h.result?.status ?? "",
      filterValue: (h) => h.result?.status ?? "",
      filterOptions: ["trusted", "untrusted", "error"],
      className: "min-w-[180px]",
      cell: ({ result: r }) =>
        r ? (
          <>
            <div className="flex flex-wrap gap-1">
              <Pill tone={RESULT_TONE[r.status][0]} dot={RESULT_TONE[r.status][1]}>
                {r.status}
              </Pill>
              {r.via_aia && <Pill>AIA</Pill>}
              {r.added.length > 0 && (
                <Pill tone="warning">
                  {r.added.length} {r.added.length === 1 ? "pin added" : "pins added"}
                </Pill>
              )}
            </div>
            {r.detail && <div className="mt-1 break-words text-xs text-ink-3">{r.detail}</div>}
          </>
        ) : (
          "—"
        ),
    },
    {
      key: "chain",
      label: "Chain served",
      hide: "phone",
      cell: ({ result: r }) => (
        <div className="flex flex-wrap items-center gap-1">
          {r?.chain.map((c, i) => {
            const name = commonName(c.subject);
            return (
              <Fragment key={`${c.spki}-${i}`}>
                {i > 0 && <ChevronRight aria-hidden="true" className="h-3.5 w-3.5 text-ink-3" />}
                <span
                  title={`${name} · valid until ${fullDate(c.not_after, true)}`}
                  className={cn(
                    "inline-flex flex-col rounded-lg px-2 py-1 text-xs leading-tight",
                    i === 0 ? "bg-badge-info text-badge-info-fg" : "bg-ground text-ink",
                    daysFromNow(c.not_after) < 45 && "ring-1 ring-inset ring-dot-warn",
                  )}
                >
                  <span className="whitespace-nowrap font-medium">{name}</span>
                  <span className="text-[11px] tabular-nums text-ink-3">{c.not_after.slice(0, 7)}</span>
                </span>
              </Fragment>
            );
          })}
        </div>
      ),
    },
  ];
  const hosts = useTable({
    rows: hostRows,
    columns: hostColumns,
    perPage: Infinity,
    initialSort: { key: "host", dir: "asc" },
  });
  const trusted = hostRows.filter((h) => h.result?.status === "trusted").length;

  return (
    <section aria-label={s.id} className="min-w-0 rounded-[14px] border border-line bg-surface">
      <div className="flex flex-wrap items-start justify-between gap-4 border-b border-line px-6 py-[18px] phone:px-4">
        <div className="flex min-w-0 flex-col gap-2">
          <h2 className="break-all font-mono text-lg font-semibold">{s.id}</h2>
          <div className="flex flex-wrap gap-1.5">
            {s.domains.map((d) => {
              const name = d.include_subdomains ? `*.${d.name}` : d.name;
              return (
                <span
                  key={name}
                  className="inline-flex min-h-6 items-center break-all rounded-full bg-soft px-2 font-mono text-xs text-ink-2"
                >
                  {name}
                </span>
              );
            })}
          </div>
          <div className="flex flex-wrap items-center gap-2.5 text-[13px] text-ink-2">
            <span className="tabular-nums">
              Expires {fullDate(s.expires_at, true)} · {left < 0 ? "expired" : `in ${left} days`}
            </span>
            <span aria-hidden="true" className="h-1.5 w-[120px] overflow-hidden rounded-full bg-divider">
              <span
                className={cn("block h-1.5 rounded-full", left < 45 ? "bg-dot-warn" : "bg-chart-1")}
                style={{ width: `${Math.min(100, Math.max(0, Math.round((left / 365) * 100)))}%` }}
              />
            </span>
          </div>
        </div>
        <div className="flex flex-wrap items-center gap-2">
          <Switch
            checked={s.auto_publish}
            disabled={busy}
            onCheckedChange={(enabled) =>
              mutate({
                path: setPath(s.id, "/auto-publish"),
                method: "PUT",
                json: { enabled },
                publishes: false,
              })
            }
            className="min-h-10 pl-1.5 pr-3"
          >
            Auto-publish
          </Switch>
          <Button variant="line" size="form" onClick={() => openDialog({ kind: "expiry", set: s })}>
            Extend expiry
          </Button>
          <Button variant="line" size="form" onClick={() => openDialog({ kind: "hosts", set: s })}>
            Edit hosts
          </Button>
          <Button variant="ink" size="form" onClick={() => openDialog({ kind: "pin", set: s })}>
            <Plus />
            Add pin
          </Button>
        </div>
      </div>

      <SubHeading title="Pins" aside={`${published} published · ${s.pins.length - published} waiting`} />
      <DataTable table={pins} rowKey={(p) => p.spki} minWidth={900} empty="No pins match" />

      <SubHeading divided title="Monitored hosts" aside={`${trusted} of ${hostRows.length} trusted`} />
      <DataTable table={hosts} rowKey={(h) => h.host} minWidth={900} empty="No hosts match" />
      <div className="h-2" />
    </section>
  );
}

function DocumentsPanel({ documents }: { documents: PinDocument[] }) {
  const columns: Column<PinDocument>[] = [
    {
      key: "serial",
      label: "Serial",
      sortValue: (d) => d.serial,
      className: "py-[11px] font-mono text-[13px] tabular-nums",
      cell: (d) => d.serial,
    },
    {
      key: "reason",
      label: "Reason",
      sortValue: (d) => d.reason,
      filterValue: (d) => d.reason,
      className: "py-[11px]",
      cell: (d) => <Pill tone={reasonTone(d.reason)}>{d.reason}</Pill>,
    },
    {
      key: "key",
      label: "Key",
      sortValue: (d) => d.key_id,
      filterValue: (d) => d.key_id,
      hide: "tablet",
      className: "py-[11px] font-mono text-xs text-ink-2",
      cell: (d) => d.key_id,
    },
    {
      key: "issued",
      label: "Issued",
      sortValue: (d) => d.issued_at,
      hide: "phone",
      className: "whitespace-nowrap py-[11px] tabular-nums text-ink-2",
      cell: (d) => <span title={new Date(d.issued_at).toLocaleString()}>{fullDate(d.issued_at)}</span>,
    },
    {
      key: "expires",
      label: "Expires",
      sortValue: (d) => d.expires_at,
      className: "whitespace-nowrap py-[11px] tabular-nums text-ink-2",
      cell: (d) => <span title={new Date(d.expires_at).toLocaleString()}>{fullDate(d.expires_at)}</span>,
    },
  ];
  const table = useTable({
    rows: documents,
    columns,
    initialSort: { key: "serial", dir: "desc" },
  });
  return (
    <Panel title="Documents" aside={<span className="text-xs text-ink-3">Every signed document published</span>}>
      {documents.length === 0 ? (
        <p className="px-6 py-8 text-center text-[13px] text-ink-3">
          No document yet. The apps use their built-in pins.
        </p>
      ) : (
        <DataTable table={table} rowKey={(d) => String(d.serial)} empty="No documents match" />
      )}
    </Panel>
  );
}
