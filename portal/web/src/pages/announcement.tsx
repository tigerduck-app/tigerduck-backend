import { useEffect, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { toast } from "sonner";
import { Megaphone, Pencil, Plus, Send } from "lucide-react";
import { api, ApiError } from "@/lib/api";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card";
import { Checkbox } from "@/components/ui/checkbox";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";
import { Textarea } from "@/components/ui/textarea";
import { PageHeader, Section } from "@/components/ui/section";
import { useEnv } from "@/hooks/use-env";
import type {
  BulletinDetail,
  BulletinList,
  BulletinSummary,
  Taxonomy,
} from "@/types/api";

type Mode = { kind: "create" } | { kind: "edit"; id: number };

type FormState = {
  title: string;
  title_clean: string;
  summary: string;
  body_clean: string;
  body_md: string;
  canonical_org: string;
  importance: string;
  content_tags: string[];
  source_url: string;
};

const EMPTY_FORM: FormState = {
  title: "",
  title_clean: "",
  summary: "",
  body_clean: "",
  body_md: "",
  canonical_org: "",
  importance: "normal",
  content_tags: [],
  source_url: "https://announce.ntust.edu.tw/manual",
};

export function AnnouncementPage() {
  const qc = useQueryClient();
  const env = useEnv();
  const isProd = env.data?.env !== "development";

  const [mode, setMode] = useState<Mode>({ kind: "create" });
  const [form, setForm] = useState<FormState>(EMPTY_FORM);
  const [confirmOpen, setConfirmOpen] = useState(false);

  const taxonomyQ = useQuery<Taxonomy>({
    queryKey: ["taxonomy"],
    queryFn: () => api<Taxonomy>("/api/announcement/taxonomy"),
    staleTime: Infinity,
  });

  const listQ = useQuery<BulletinList>({
    queryKey: ["bulletins-list"],
    queryFn: () => api<BulletinList>("/api/announcement/list?limit=30"),
  });

  // Default canonical_org once taxonomy loads (matches the prior page).
  useEffect(() => {
    if (
      mode.kind === "create" &&
      !form.canonical_org &&
      taxonomyQ.data?.orgs.length
    ) {
      setForm((f) => ({ ...f, canonical_org: taxonomyQ.data!.orgs[0].id }));
    }
  }, [taxonomyQ.data, form.canonical_org, mode.kind]);

  const editQ = useQuery<BulletinDetail>({
    queryKey: ["bulletin", mode.kind === "edit" ? mode.id : null],
    enabled: mode.kind === "edit",
    queryFn: () =>
      mode.kind === "edit"
        ? api<BulletinDetail>(`/api/announcement/${mode.id}`)
        : Promise.reject("not-edit"),
  });

  useEffect(() => {
    if (mode.kind === "edit" && editQ.data) {
      const b = editQ.data;
      setForm({
        title: b.title,
        title_clean: b.title_clean ?? "",
        summary: b.summary ?? "",
        body_clean: b.body_clean ?? "",
        body_md: b.body_md ?? "",
        canonical_org: b.canonical_org ?? "",
        importance: b.importance ?? "normal",
        content_tags: b.content_tags,
        source_url: b.source_url,
      });
    }
  }, [editQ.data, mode.kind]);

  const createMut = useMutation({
    mutationFn: (body: Record<string, unknown>) =>
      api<BulletinDetail>("/api/announcement", {
        method: "POST",
        json: body,
      }),
    onSuccess: (b) => {
      toast.success(`Broadcast #${b.id}`);
      qc.invalidateQueries({ queryKey: ["bulletins-list"] });
      setForm(EMPTY_FORM);
      setMode({ kind: "create" });
    },
    onError: (e) => toast.error(asMessage(e)),
  });

  const updateMut = useMutation({
    mutationFn: (args: { id: number; body: Record<string, unknown> }) =>
      api<BulletinDetail>(`/api/announcement/${args.id}`, {
        method: "PATCH",
        json: args.body,
      }),
    onSuccess: (b) => {
      toast.success(`Updated #${b.id}`);
      qc.invalidateQueries({ queryKey: ["bulletins-list"] });
      qc.invalidateQueries({ queryKey: ["bulletin", b.id] });
    },
    onError: (e) => toast.error(asMessage(e)),
  });

  function trimOrUndef(s: string): string | undefined {
    const v = s.trim();
    return v ? v : undefined;
  }

  function submit() {
    if (mode.kind === "create") {
      const body: Record<string, unknown> = {
        title: form.title,
        title_clean: trimOrUndef(form.title_clean),
        summary: trimOrUndef(form.summary),
        body_clean: trimOrUndef(form.body_clean),
        body_md: trimOrUndef(form.body_md),
        canonical_org: form.canonical_org,
        content_tags: form.content_tags,
        importance: form.importance,
        source_url: form.source_url,
      };
      // Drop undefined keys so the server's extra=forbid + length
      // validators don't see explicit nulls for optional strings.
      Object.keys(body).forEach((k) => body[k] === undefined && delete body[k]);
      createMut.mutate(body);
    } else {
      // PATCH semantics: send a key only if it has changed relative to
      // the loaded bulletin. content_tags is always sent because an
      // empty list is a legitimate "clear all tags" intent.
      const original = editQ.data!;
      const body: Record<string, unknown> = { content_tags: form.content_tags };
      if (form.title !== original.title) body.title = form.title;
      if (form.title_clean !== (original.title_clean ?? ""))
        body.title_clean = trimOrUndef(form.title_clean);
      if (form.summary !== (original.summary ?? ""))
        body.summary = trimOrUndef(form.summary);
      if (form.body_clean !== (original.body_clean ?? ""))
        body.body_clean = trimOrUndef(form.body_clean);
      if (form.body_md !== (original.body_md ?? ""))
        body.body_md = trimOrUndef(form.body_md);
      if (form.canonical_org !== original.canonical_org)
        body.canonical_org = form.canonical_org;
      if (form.importance !== original.importance)
        body.importance = form.importance;
      updateMut.mutate({ id: mode.id, body });
    }
  }

  function handleSubmit(e: React.FormEvent) {
    e.preventDefault();
    if (isProd) {
      setConfirmOpen(true);
    } else {
      submit();
    }
  }

  const pending = createMut.isPending || updateMut.isPending;
  const tags = taxonomyQ.data?.tags ?? [];
  const orgs = taxonomyQ.data?.orgs ?? [];
  const importance: { id: string; label: string }[] = [
    { id: "low", label: "Low" },
    { id: "normal", label: "Normal" },
    { id: "high", label: "High" },
  ];

  return (
    <div className="space-y-8">
      <PageHeader
        title="Announcement"
        description={
          isProd ? (
            <span>
              <Badge variant="warning" className="mr-2">
                production
              </Badge>
              Mutations confirm before broadcasting to every subscribed
              device.
            </span>
          ) : (
            "Compose a bulletin or edit an LLM-generated one. Pushes go out on the next dispatcher tick."
          )
        }
      />

      <Card>
        <CardHeader>
          <div className="flex items-center justify-between gap-2">
            <div className="flex items-center gap-2">
              <Megaphone className="h-5 w-5 text-muted-foreground" />
              <CardTitle>
                {mode.kind === "create"
                  ? "New announcement"
                  : `Edit bulletin #${mode.id}`}
              </CardTitle>
            </div>
            {mode.kind === "edit" && (
              <Button
                variant="ghost"
                size="sm"
                onClick={() => {
                  setMode({ kind: "create" });
                  setForm({
                    ...EMPTY_FORM,
                    canonical_org: orgs[0]?.id ?? "",
                  });
                }}
              >
                <Plus className="mr-1 h-4 w-4" />
                New
              </Button>
            )}
          </div>
          <CardDescription>
            Title is what shows on the lock screen. Body fields land in the
            Announcement tab inside the iOS app.
          </CardDescription>
        </CardHeader>
        <CardContent>
          <form className="space-y-5" onSubmit={handleSubmit}>
            <div className="grid gap-1.5">
              <Label htmlFor="title">Title</Label>
              <Input
                id="title"
                value={form.title}
                onChange={(e) => setForm({ ...form, title: e.target.value })}
                required
                maxLength={500}
              />
            </div>

            <div className="grid gap-1.5">
              <Label htmlFor="title_clean">Title (clean)</Label>
              <Input
                id="title_clean"
                value={form.title_clean}
                onChange={(e) =>
                  setForm({ ...form, title_clean: e.target.value })
                }
                maxLength={200}
                placeholder="Stripped version for in-app display (optional)"
              />
            </div>

            <div className="grid gap-1.5">
              <Label htmlFor="summary">Summary</Label>
              <Textarea
                id="summary"
                rows={2}
                value={form.summary}
                onChange={(e) =>
                  setForm({ ...form, summary: e.target.value })
                }
                maxLength={2000}
              />
            </div>

            <div className="grid gap-3 md:grid-cols-2">
              <div className="grid gap-1.5">
                <Label>Organization</Label>
                <Select
                  value={form.canonical_org}
                  onValueChange={(v) =>
                    setForm({ ...form, canonical_org: v })
                  }
                >
                  <SelectTrigger>
                    <SelectValue placeholder="Select org" />
                  </SelectTrigger>
                  <SelectContent>
                    {orgs.map((o) => (
                      <SelectItem key={o.id} value={o.id}>
                        {o.label}
                      </SelectItem>
                    ))}
                  </SelectContent>
                </Select>
              </div>
              <div className="grid gap-1.5">
                <Label>Importance</Label>
                <Select
                  value={form.importance}
                  onValueChange={(v) => setForm({ ...form, importance: v })}
                >
                  <SelectTrigger>
                    <SelectValue />
                  </SelectTrigger>
                  <SelectContent>
                    {importance.map((i) => (
                      <SelectItem key={i.id} value={i.id}>
                        {i.label}
                      </SelectItem>
                    ))}
                  </SelectContent>
                </Select>
              </div>
            </div>

            <div className="grid gap-1.5">
              <Label>Tags (max 8)</Label>
              <div className="flex flex-wrap gap-2">
                {tags.map((t) => {
                  const checked = form.content_tags.includes(t.id);
                  return (
                    <label
                      key={t.id}
                      className={
                        "flex cursor-pointer items-center gap-2 rounded-full border px-3 py-1 text-xs " +
                        (checked
                          ? "border-primary/40 bg-primary/10 text-primary"
                          : "border-border hover:bg-accent")
                      }
                    >
                      <Checkbox
                        checked={checked}
                        onCheckedChange={(v) => {
                          const isOn = v === true;
                          setForm((f) => ({
                            ...f,
                            content_tags: isOn
                              ? Array.from(
                                  new Set([...f.content_tags, t.id]),
                                ).slice(0, 8)
                              : f.content_tags.filter((x) => x !== t.id),
                          }));
                        }}
                      />
                      {t.label}
                    </label>
                  );
                })}
              </div>
            </div>

            <div className="grid gap-1.5">
              <Label htmlFor="body_clean">Body (plain)</Label>
              <Textarea
                id="body_clean"
                rows={5}
                value={form.body_clean}
                onChange={(e) =>
                  setForm({ ...form, body_clean: e.target.value })
                }
                maxLength={20000}
              />
            </div>

            <div className="grid gap-1.5">
              <Label htmlFor="body_md">Body (markdown)</Label>
              <Textarea
                id="body_md"
                rows={5}
                value={form.body_md}
                onChange={(e) =>
                  setForm({ ...form, body_md: e.target.value })
                }
                maxLength={20000}
              />
            </div>

            <div className="grid gap-1.5">
              <Label htmlFor="source_url">Source URL</Label>
              <Input
                id="source_url"
                value={form.source_url}
                onChange={(e) =>
                  setForm({ ...form, source_url: e.target.value })
                }
                maxLength={1000}
              />
            </div>

            <div className="flex gap-2">
              <Button type="submit" disabled={pending}>
                <Send className="mr-1 h-4 w-4" />
                {pending
                  ? "Working…"
                  : mode.kind === "create"
                    ? "Broadcast"
                    : "Save changes"}
              </Button>
              {mode.kind === "edit" && (
                <Button
                  type="button"
                  variant="ghost"
                  onClick={() => {
                    setMode({ kind: "create" });
                    setForm({
                      ...EMPTY_FORM,
                      canonical_org: orgs[0]?.id ?? "",
                    });
                  }}
                >
                  Cancel
                </Button>
              )}
            </div>
          </form>
        </CardContent>
      </Card>

      <Section
        title="Recent bulletins"
        actions={
          <Button
            variant="outline"
            size="sm"
            onClick={() =>
              qc.invalidateQueries({ queryKey: ["bulletins-list"] })
            }
          >
            Refresh
          </Button>
        }
      >
        {listQ.isLoading && (
          <div className="text-sm text-muted-foreground">Loading…</div>
        )}
        {listQ.data && listQ.data.items.length === 0 && (
          <div className="text-sm text-muted-foreground">No bulletins yet.</div>
        )}
        {listQ.data && listQ.data.items.length > 0 && (
          <Table>
            <TableHeader>
              <TableRow>
                <TableHead>#</TableHead>
                <TableHead>Source</TableHead>
                <TableHead>Org</TableHead>
                <TableHead>Title</TableHead>
                <TableHead>Tags</TableHead>
                <TableHead>Posted</TableHead>
                <TableHead className="w-16"></TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {listQ.data.items.map((b) => (
                <BulletinRow
                  key={b.id}
                  bulletin={b}
                  onEdit={() => setMode({ kind: "edit", id: b.id })}
                />
              ))}
            </TableBody>
          </Table>
        )}
      </Section>

      <Dialog open={confirmOpen} onOpenChange={setConfirmOpen}>
        <DialogContent>
          <DialogHeader>
            <DialogTitle>
              {mode.kind === "create"
                ? "Broadcast announcement?"
                : `Modify bulletin #${mode.kind === "edit" ? mode.id : ""}?`}
            </DialogTitle>
            <DialogDescription>
              You're in <Badge variant="warning">production</Badge>. This
              is visible to every subscribed device.
            </DialogDescription>
          </DialogHeader>
          <DialogFooter>
            <Button variant="ghost" onClick={() => setConfirmOpen(false)}>
              Cancel
            </Button>
            <Button
              variant="destructive"
              onClick={() => {
                setConfirmOpen(false);
                submit();
              }}
            >
              {mode.kind === "create" ? "Broadcast" : "Save"}
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </div>
  );
}

function BulletinRow({
  bulletin: b,
  onEdit,
}: {
  bulletin: BulletinSummary;
  onEdit: () => void;
}) {
  return (
    <TableRow>
      <TableCell className="tabular-nums">{b.id}</TableCell>
      <TableCell>
        <Badge
          variant={b.source === "manual" ? "default" : "secondary"}
          className="text-[10px] uppercase"
        >
          {b.source}
        </Badge>
      </TableCell>
      <TableCell className="font-mono text-xs">{b.canonical_org}</TableCell>
      <TableCell className="max-w-md truncate">{b.title}</TableCell>
      <TableCell>
        <div className="flex flex-wrap gap-1">
          {b.content_tags.map((t) => (
            <Badge key={t} variant="muted" className="text-[10px]">
              {t}
            </Badge>
          ))}
        </div>
      </TableCell>
      <TableCell className="text-xs text-muted-foreground">
        {b.posted_at ? b.posted_at.replace("T", " ").slice(0, 16) : "—"}
      </TableCell>
      <TableCell>
        <Button size="sm" variant="ghost" onClick={onEdit}>
          <Pencil className="h-4 w-4" />
        </Button>
      </TableCell>
    </TableRow>
  );
}

function asMessage(e: unknown): string {
  if (e instanceof ApiError) return `HTTP ${e.status} — ${e.message}`;
  if (e instanceof Error) return e.message;
  return String(e);
}
