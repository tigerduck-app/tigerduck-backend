// Links: the redirects the API serves at /links/<name>. The backend
// validates and audits every change; this page only edits and shows them.

import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { toast } from "sonner";
import { Link2, Pencil, Play, Plus } from "lucide-react";
import { api, ApiError } from "@/lib/api";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent } from "@/components/ui/card";
import {
  Dialog,
  DialogContent,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { PageHeader } from "@/components/ui/section";
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
import type { LinkRow, LinkTestResult } from "@/types/api";
import { asMessage, formatTs } from "./lists/format";

const RULES_EXAMPLE = `[
  {"type": "status", "equals": 200},
  {"type": "final_host", "in": ["www.academic.ntust.edu.tw"]},
  {"type": "href_suffix_count", "suffix": ".ics", "min": 1}
]`;

type Draft = {
  name: string;
  target_url: string;
  check_interval_minutes: string;
  expectations: string;
};

const EMPTY: Draft = {
  name: "",
  target_url: "",
  check_interval_minutes: "60",
  expectations: "",
};

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

function CheckBadge({ link }: { link: LinkRow }) {
  if (!link.enabled) return <Badge variant="muted">Disabled</Badge>;
  if (link.last_check_ok === null) return <Badge variant="muted">Not checked</Badge>;
  if (link.last_check_ok) return <Badge variant="success">OK</Badge>;
  return <Badge variant="destructive">Failing</Badge>;
}

export function LinksPage() {
  const qc = useQueryClient();
  // null: closed; "" : a new link; otherwise the name being edited.
  const [editing, setEditing] = useState<string | null>(null);
  const [draft, setDraft] = useState<Draft>(EMPTY);

  const linksQ = useQuery<{ links: LinkRow[] }>({
    queryKey: ["links"],
    queryFn: () => api<{ links: LinkRow[] }>("/api/links"),
    // Scheduled checks change the rows while the page is open.
    refetchInterval: 15_000,
  });
  const refresh = () => qc.invalidateQueries({ queryKey: ["links"] });

  const saveMut = useMutation({
    mutationFn: async () => {
      let expectations: unknown;
      try {
        expectations = JSON.parse(draft.expectations || "[]");
      } catch {
        throw new Error("Rules must be a JSON array");
      }
      const body = {
        target_url: draft.target_url.trim(),
        check_interval_minutes: Number(draft.check_interval_minutes),
        expectations,
      };
      return editing
        ? api<LinkRow>(`/api/links/${editing}`, { method: "PATCH", json: body })
        : api<LinkRow>("/api/links", {
            method: "POST",
            json: { name: draft.name.trim(), ...body },
          });
    },
    onSuccess: (link) => {
      toast.success(`${editing ? "Saved" : "Created"} ${link.name}`);
      setEditing(null);
      refresh();
    },
    onError: (e) => toast.error(errorText(e)),
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
    setDraft(
      link
        ? {
            name: link.name,
            target_url: link.target_url,
            check_interval_minutes: String(link.check_interval_minutes),
            expectations: JSON.stringify(link.expectations, null, 2),
          }
        : EMPTY,
    );
    setEditing(link ? link.name : "");
  };

  const links = linksQ.data?.links;

  return (
    <div className="space-y-6">
      <PageHeader
        title="Links"
        description="Redirects served at /links/<name>."
        actions={
          <Button onClick={() => openEditor(null)}>
            <Plus className="mr-1 h-4 w-4" />
            New link
          </Button>
        }
      />

      {linksQ.isLoading && <Skeleton className="h-40" />}
      {linksQ.isError && (
        <Card>
          <CardContent className="text-sm text-destructive">
            Failed to load links: {errorText(linksQ.error)}
          </CardContent>
        </Card>
      )}
      {links && links.length === 0 && (
        <Card>
          <CardContent className="flex items-center gap-2 text-sm text-muted-foreground">
            <Link2 className="h-4 w-4" />
            No links yet.
          </CardContent>
        </Card>
      )}
      {links && links.length > 0 && (
        <Table>
          <TableHeader>
            <TableRow>
              <TableHead>Name</TableHead>
              <TableHead>Target</TableHead>
              <TableHead>Check</TableHead>
              <TableHead>Last checked</TableHead>
              <TableHead className="text-right" />
            </TableRow>
          </TableHeader>
          <TableBody>
            {links.map((link) => (
              <TableRow key={link.name}>
                <TableCell
                  className="max-w-[10rem] truncate font-mono text-sm font-medium"
                  title={link.name}
                >
                  {link.name}
                </TableCell>
                <TableCell className="max-w-[12rem] lg:max-w-xs">
                  <a
                    href={link.target_url}
                    target="_blank"
                    rel="noreferrer noopener"
                    className="block truncate text-sm text-primary underline-offset-4 hover:underline"
                    title={link.target_url}
                  >
                    {link.target_url}
                  </a>
                </TableCell>
                <TableCell className="max-w-[14rem]">
                  <CheckBadge link={link} />
                  {link.last_check_detail && (
                    <div
                      className="mt-1 truncate text-xs text-muted-foreground"
                      title={link.last_check_detail}
                    >
                      {link.last_check_detail}
                    </div>
                  )}
                </TableCell>
                <TableCell className="whitespace-nowrap text-xs text-muted-foreground">
                  <div>{link.last_check_at ? formatTs(link.last_check_at) : "—"}</div>
                  <div>every {link.check_interval_minutes} min</div>
                </TableCell>
                <TableCell>
                  <div className="flex justify-end gap-1 whitespace-nowrap">
                    <Button
                      variant="outline"
                      size="sm"
                      disabled={testMut.isPending}
                      onClick={() => testMut.mutate(link.name)}
                    >
                      <Play className="mr-1 h-3.5 w-3.5" />
                      {testMut.isPending && testMut.variables === link.name
                        ? "Testing…"
                        : "Test"}
                    </Button>
                    <Button
                      variant="ghost"
                      size="sm"
                      disabled={toggleMut.isPending}
                      onClick={() => toggleMut.mutate(link)}
                    >
                      {link.enabled ? "Disable" : "Enable"}
                    </Button>
                    <Button
                      variant="ghost"
                      size="icon"
                      className="h-8 w-8"
                      title="Edit"
                      aria-label={`Edit ${link.name}`}
                      onClick={() => openEditor(link)}
                    >
                      <Pencil className="h-4 w-4" />
                    </Button>
                  </div>
                </TableCell>
              </TableRow>
            ))}
          </TableBody>
        </Table>
      )}

      <Dialog
        open={editing !== null}
        onOpenChange={(open) => !open && setEditing(null)}
      >
        <DialogContent>
          <DialogHeader>
            <DialogTitle>{editing ? `Edit ${editing}` : "New link"}</DialogTitle>
          </DialogHeader>
          <div className="space-y-4">
            {!editing && (
              <div className="grid gap-1.5">
                <Label htmlFor="link-name">Name</Label>
                <Input
                  id="link-name"
                  value={draft.name}
                  onChange={(e) => setDraft({ ...draft, name: e.target.value })}
                  placeholder="ntust-calendar"
                  maxLength={64}
                  autoFocus
                />
              </div>
            )}
            <div className="grid gap-1.5">
              <Label htmlFor="link-target">Target URL</Label>
              <Input
                id="link-target"
                value={draft.target_url}
                onChange={(e) => setDraft({ ...draft, target_url: e.target.value })}
                placeholder="https://www.academic.ntust.edu.tw/…"
                maxLength={2048}
              />
            </div>
            <div className="grid gap-1.5">
              <Label htmlFor="link-interval">Check every (minutes)</Label>
              <Input
                id="link-interval"
                type="number"
                min={5}
                max={1440}
                value={draft.check_interval_minutes}
                onChange={(e) =>
                  setDraft({ ...draft, check_interval_minutes: e.target.value })
                }
              />
            </div>
            <div className="grid gap-1.5">
              <Label htmlFor="link-rules">Rules (JSON)</Label>
              <Textarea
                id="link-rules"
                rows={6}
                className="font-mono text-xs"
                value={draft.expectations}
                onChange={(e) => setDraft({ ...draft, expectations: e.target.value })}
                placeholder={RULES_EXAMPLE}
              />
            </div>
          </div>
          <DialogFooter>
            <Button variant="ghost" onClick={() => setEditing(null)}>
              Cancel
            </Button>
            <Button
              disabled={
                saveMut.isPending ||
                !draft.target_url.trim() ||
                (!editing && !draft.name.trim())
              }
              onClick={() => saveMut.mutate()}
            >
              {editing ? "Save" : "Create"}
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </div>
  );
}
