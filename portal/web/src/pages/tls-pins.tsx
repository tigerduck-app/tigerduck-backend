// TLS pins: the sets the apps' pin document is signed from, what the
// watcher last saw on each host, and every document published. Changes go
// through the backend, which holds the signing key and audits each one.

import { useRef, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { toast } from "sonner";
import { Plus, RefreshCw, Send, Trash2 } from "lucide-react";
import { api, ApiError } from "@/lib/api";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent } from "@/components/ui/card";
import { Checkbox } from "@/components/ui/checkbox";
import {
  Dialog,
  DialogContent,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { PageHeader, Section } from "@/components/ui/section";
import { Skeleton } from "@/components/ui/skeleton";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";
import { Textarea } from "@/components/ui/textarea";

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

type DraftFields =
  | { kind: "pin"; set: string; spki: string; label: string }
  | { kind: "hosts"; set: string; hosts: string }
  | { kind: "expiry"; set: string; date: string };
// `key` tells dialogs apart, so a late answer closes only the one that sent it.
type Draft = DraftFields & { key: number };

function day(iso: string | null): string {
  return iso ? iso.slice(0, 10) : "—";
}

function time(iso: string): string {
  return new Date(iso).toLocaleString();
}

const STATUS_VARIANT = {
  trusted: "success",
  untrusted: "destructive",
  error: "warning",
} as const;

// Publish now approves only the pending pins on screen, not ones found since.
const shownPending = (data?: TlsPinsResponse) =>
  Object.fromEntries(
    (data?.sets ?? []).map((s) => [s.id, s.pins.filter((p) => p.pending).map((p) => p.spki)]),
  );

export function TlsPinsPage() {
  const qc = useQueryClient();
  const [draft, setDraft] = useState<Draft | null>(null);
  const openedDialogs = useRef(0);
  const openDraft = (fields: DraftFields) => {
    openedDialogs.current += 1;
    setDraft({ ...fields, key: openedDialogs.current });
  };

  const pinsQ = useQuery<TlsPinsResponse>({
    queryKey: ["tls-pins"],
    queryFn: () => api<TlsPinsResponse>("/api/tls-pins"),
  });

  const action = useMutation({
    mutationFn: (a: {
      path: string;
      method: "POST" | "PUT";
      json?: unknown;
      publishes: boolean;
      dialog?: number;
    }) =>
      api<ActionResult>(`/api/tls-pins${a.path}`, {
        method: a.method,
        json: a.json,
      }).then((r) => ({ ...r, publishes: a.publishes })),
    onSuccess: (r, a) => {
      // Another dialog opened meanwhile stays open.
      setDraft((current) => (current?.key === a.dialog ? null : current));
      if (!r.publishes) toast.success("Saved");
      else if (r.serial !== null) toast.success(`Published serial ${r.serial}`);
      else toast.warning("Saved, not published: no signing key");
      qc.invalidateQueries({ queryKey: ["tls-pins"] });
    },
    onError: (e: unknown) =>
      toast.error(e instanceof ApiError ? e.message : "Request failed"),
  });

  const setPath = (id: string, rest: string) =>
    `/sets/${encodeURIComponent(id)}${rest}`;

  const save = () => {
    if (!draft) return;
    if (draft.kind === "pin") {
      action.mutate({
        path: setPath(draft.set, "/pins"),
        method: "POST",
        json: { spki: draft.spki.trim(), label: draft.label.trim() },
        publishes: true,
        dialog: draft.key,
      });
    } else if (draft.kind === "hosts") {
      action.mutate({
        path: setPath(draft.set, "/monitored-hosts"),
        method: "PUT",
        json: {
          hosts: draft.hosts
            .split("\n")
            .map((h) => h.trim())
            .filter(Boolean),
        },
        publishes: false,
        dialog: draft.key,
      });
    } else {
      action.mutate({
        path: setPath(draft.set, "/expires-at"),
        method: "PUT",
        json: { expires_at: `${draft.date}T00:00:00Z` },
        publishes: true,
        dialog: draft.key,
      });
    }
  };

  const data = pinsQ.data;

  return (
    <div className="space-y-6">
      <PageHeader
        title="TLS pins"
        description="Signed into /.well-known/tigerduck/tls-pins.json, which the apps verify. The watcher checks every monitored host hourly."
        actions={
          <>
            <Button
              size="sm"
              variant="outline"
              disabled={action.isPending}
              onClick={() =>
                action.mutate({ path: "/resign", method: "POST", publishes: true })
              }
            >
              <RefreshCw className="mr-1 h-4 w-4" /> Re-sign now
            </Button>
            <Button
              size="sm"
              disabled={action.isPending}
              onClick={() =>
                action.mutate({
                  path: "/publish",
                  method: "POST",
                  json: { approve: shownPending(data) },
                  publishes: true,
                })
              }
            >
              <Send className="mr-1 h-4 w-4" /> Publish now
            </Button>
          </>
        }
      />

      {pinsQ.isLoading && <Skeleton className="h-48 w-full" />}
      {pinsQ.isError && (
        <Card>
          <CardContent className="text-sm text-destructive">
            Failed to load TLS pins: {(pinsQ.error as Error).message}
          </CardContent>
        </Card>
      )}
      {data?.sets.map((s) => (
        <Section
          key={s.id}
          title={s.id}
          description={`${s.domains
            .map((d) => (d.include_subdomains ? `*.${d.name}` : d.name))
            .join(", ")} · expires ${day(s.expires_at)}`}
          actions={
            <div className="flex flex-wrap items-center gap-2">
              <Label className="flex items-center gap-2 text-sm">
                <Checkbox
                  checked={s.auto_publish}
                  disabled={action.isPending}
                  onCheckedChange={(v) =>
                    action.mutate({
                      path: setPath(s.id, "/auto-publish"),
                      method: "PUT",
                      json: { enabled: v === true },
                      publishes: false,
                    })
                  }
                />
                Auto-publish
              </Label>
              <Button
                size="sm"
                variant="outline"
                onClick={() =>
                  openDraft({ kind: "expiry", set: s.id, date: day(s.expires_at) })
                }
              >
                Extend expiry
              </Button>
              <Button
                size="sm"
                variant="outline"
                onClick={() =>
                  openDraft({
                    kind: "hosts",
                    set: s.id,
                    hosts: s.monitored_hosts.join("\n"),
                  })
                }
              >
                Edit hosts
              </Button>
              <Button
                size="sm"
                onClick={() => openDraft({ kind: "pin", set: s.id, spki: "", label: "" })}
              >
                <Plus className="mr-1 h-4 w-4" /> Add pin
              </Button>
            </div>
          }
        >
          <Table>
            <TableHeader>
              <TableRow>
                <TableHead>Label</TableHead>
                <TableHead>Pin</TableHead>
                <TableHead>Source</TableHead>
                <TableHead>First seen</TableHead>
                <TableHead>Last seen</TableHead>
                <TableHead className="w-12" />
              </TableRow>
            </TableHeader>
            <TableBody>
              {s.pins.map((p) => (
                <TableRow key={p.spki}>
                  <TableCell className="min-w-40 max-w-64 break-words">
                    {p.label}
                    {p.pending && (
                      <Badge variant="warning" className="ml-2">
                        not published
                      </Badge>
                    )}
                  </TableCell>
                  <TableCell className="min-w-72 font-mono text-xs break-all">{p.spki}</TableCell>
                  <TableCell>{p.source}</TableCell>
                  <TableCell className="whitespace-nowrap">{day(p.first_seen_at)}</TableCell>
                  <TableCell className="whitespace-nowrap">
                    {day(p.last_seen_at)}
                    {p.stale && (
                      <Badge variant="warning" className="ml-2">
                        unseen 90 days
                      </Badge>
                    )}
                  </TableCell>
                  <TableCell className="text-right">
                    <Button
                      variant="ghost"
                      size="icon"
                      disabled={
                        action.isPending ||
                        (!p.pending && s.pins.filter((q) => !q.pending).length === 1)
                      }
                      onClick={() => {
                        if (!confirm(`Remove ${p.label} from ${s.id} and publish?`)) return;
                        action.mutate({
                          path: setPath(s.id, "/pins/remove"),
                          method: "POST",
                          json: { spki: p.spki },
                          publishes: true,
                        });
                      }}
                    >
                      <Trash2 className="h-4 w-4" />
                    </Button>
                  </TableCell>
                </TableRow>
              ))}
            </TableBody>
          </Table>

          <Table>
            <TableHeader>
              <TableRow>
                <TableHead>Host</TableHead>
                <TableHead>Checked</TableHead>
                <TableHead>Result</TableHead>
                <TableHead>Chain</TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {s.monitored_hosts.map((host) => {
                const r = s.watch_results[host];
                return (
                  <TableRow key={host}>
                    <TableCell className="whitespace-nowrap font-medium">{host}</TableCell>
                    <TableCell className="whitespace-nowrap">
                      {r ? time(r.checked_at) : "—"}
                    </TableCell>
                    <TableCell className="min-w-40">
                      {r && (
                        <div className="space-y-1">
                          <Badge variant={STATUS_VARIANT[r.status]}>{r.status}</Badge>
                          {r.via_aia && <Badge variant="muted">AIA</Badge>}
                          {r.added.length > 0 && (
                            <Badge variant="warning">{r.added.length} pins added</Badge>
                          )}
                          {r.detail && (
                            <div className="text-xs text-muted-foreground break-words">
                              {r.detail}
                            </div>
                          )}
                        </div>
                      )}
                    </TableCell>
                    <TableCell className="min-w-80 text-xs">
                      {r?.chain.map((c) => (
                        <div key={c.spki} className="break-words">
                          {c.subject}
                          <span className="ml-1 text-muted-foreground">
                            until {day(c.not_after)}
                          </span>
                        </div>
                      ))}
                    </TableCell>
                  </TableRow>
                );
              })}
            </TableBody>
          </Table>
        </Section>
      ))}

      {data && (
        <Section title="Documents">
          {data.documents.length === 0 ? (
            <p className="py-4 text-sm text-muted-foreground">
              No document yet. The apps use their built-in pins.
            </p>
          ) : (
            <Table>
              <TableHeader>
                <TableRow>
                  <TableHead>Serial</TableHead>
                  <TableHead>Reason</TableHead>
                  <TableHead>Key</TableHead>
                  <TableHead>Issued</TableHead>
                  <TableHead>Expires</TableHead>
                </TableRow>
              </TableHeader>
              <TableBody>
                {data.documents.map((d) => (
                  <TableRow key={d.serial}>
                    <TableCell className="font-mono text-xs">{d.serial}</TableCell>
                    <TableCell>{d.reason}</TableCell>
                    <TableCell>{d.key_id}</TableCell>
                    <TableCell className="whitespace-nowrap">{time(d.issued_at)}</TableCell>
                    <TableCell className="whitespace-nowrap">{time(d.expires_at)}</TableCell>
                  </TableRow>
                ))}
              </TableBody>
            </Table>
          )}
        </Section>
      )}

      <Dialog open={draft !== null} onOpenChange={(o) => !o && setDraft(null)}>
        <DialogContent>
          <DialogHeader>
            <DialogTitle>
              {draft?.kind === "pin"
                ? `Add pin to ${draft.set}`
                : draft?.kind === "hosts"
                  ? `Monitored hosts of ${draft.set}`
                  : `Extend ${draft?.set}`}
            </DialogTitle>
          </DialogHeader>
          {draft?.kind === "pin" && (
            <div className="space-y-3">
              <div className="space-y-1.5">
                <Label htmlFor="pin-spki">SPKI SHA-256 (base64)</Label>
                <Input
                  id="pin-spki"
                  className="font-mono text-xs"
                  value={draft.spki}
                  onChange={(e) => setDraft({ ...draft, spki: e.target.value })}
                />
              </div>
              <div className="space-y-1.5">
                <Label htmlFor="pin-label">Label</Label>
                <Input
                  id="pin-label"
                  value={draft.label}
                  onChange={(e) => setDraft({ ...draft, label: e.target.value })}
                />
              </div>
            </div>
          )}
          {draft?.kind === "hosts" && (
            <div className="space-y-1.5">
              <Label htmlFor="pin-hosts">One host or host:port per line</Label>
              <Textarea
                id="pin-hosts"
                rows={8}
                className="font-mono text-xs"
                value={draft.hosts}
                onChange={(e) => setDraft({ ...draft, hosts: e.target.value })}
              />
            </div>
          )}
          {draft?.kind === "expiry" && (
            <div className="space-y-1.5">
              <Label htmlFor="pin-expiry">New expiry (UTC)</Label>
              <Input
                id="pin-expiry"
                type="date"
                value={draft.date}
                onChange={(e) => setDraft({ ...draft, date: e.target.value })}
              />
            </div>
          )}
          <DialogFooter>
            <Button variant="outline" onClick={() => setDraft(null)}>
              Cancel
            </Button>
            <Button disabled={action.isPending} onClick={save}>
              Save
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </div>
  );
}
