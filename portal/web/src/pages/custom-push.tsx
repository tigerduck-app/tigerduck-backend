import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { toast } from "sonner";
import { Loader2, Megaphone, Send, Plus, Minus } from "lucide-react";
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
  CustomPushPreviewResponse,
  CustomPushRecentItem,
  CustomPushRequest,
  CustomPushSendResponse,
  CustomPushTargetClass,
} from "@/types/api";

// Matches the portal's other pages (e.g. announcement.tsx uses
// "/api/announcement/..."). The portal app proxies /api/custom-push/*
// → backend /v2/custom-push/* once that route is wired in portal/app.
const API_PREFIX = "/api";

type FormState = {
  title: string;
  body: string;
  keeps_record: boolean;
  force_ring: boolean;
  target_iphone: boolean;
  target_ipad: boolean;
  target_android: boolean;
  user_id: string;
  device_id: string;
  show_advanced: boolean;
};

const EMPTY: FormState = {
  title: "",
  body: "",
  keeps_record: true,
  force_ring: true,
  target_iphone: true,
  target_ipad: true,
  target_android: true,
  user_id: "",
  device_id: "",
  show_advanced: false,
};

function selectedClasses(f: FormState): CustomPushTargetClass[] {
  const out: CustomPushTargetClass[] = [];
  if (f.target_iphone) out.push("iphone");
  if (f.target_ipad) out.push("ipad");
  if (f.target_android) out.push("android");
  return out;
}

function buildRequest(f: FormState): CustomPushRequest {
  const req: CustomPushRequest = {
    target_classes: selectedClasses(f),
    title: f.title,
    body: f.body,
    keeps_record: f.keeps_record,
    force_ring: f.force_ring,
  };
  const u = f.user_id.trim();
  const d = f.device_id.trim();
  if (u) req.user_id = u;
  if (d) req.device_id = d;
  return req;
}

export function CustomPushPage() {
  const env = useEnv();
  const isProd = env.data?.env !== "development";
  const qc = useQueryClient();

  const [form, setForm] = useState<FormState>(EMPTY);
  const [confirmOpen, setConfirmOpen] = useState(false);
  const [previewCounts, setPreviewCounts] =
    useState<Record<string, number> | null>(null);

  const recentQ = useQuery<CustomPushRecentItem[]>({
    queryKey: ["custom-push-recent"],
    queryFn: () =>
      api<CustomPushRecentItem[]>(`${API_PREFIX}/custom-push/recent?limit=30`),
    // Live-update without manual refresh. 2s while anything is still
    // queueing (scheduler tick is on the order of seconds, so the
    // operator sees total/sent_at fill in quickly) and 10s otherwise so
    // sends from other browser tabs / direct API calls also show up.
    refetchInterval: (q) =>
      q.state.data?.some((r) => r.is_queueing) ? 2000 : 10_000,
    refetchOnWindowFocus: true,
  });

  const previewMut = useMutation({
    mutationFn: () =>
      api<CustomPushPreviewResponse>(`${API_PREFIX}/custom-push/preview`, {
        method: "POST",
        json: {
          target_classes: selectedClasses(form),
          ...(form.user_id.trim() ? { user_id: form.user_id.trim() } : {}),
          ...(form.device_id.trim() ? { device_id: form.device_id.trim() } : {}),
        },
      }),
    onSuccess: (r) => setPreviewCounts(r.matched),
    onError: (e) => toast.error(asMessage(e)),
  });

  const sendMut = useMutation({
    mutationFn: () =>
      api<CustomPushSendResponse>(`${API_PREFIX}/custom-push`, {
        method: "POST",
        json: buildRequest(form),
      }),
    onSuccess: (r) => {
      toast.success(
        `Pushed (${r.kind}) → request ${r.request_id}, queued ${r.queued}`,
      );
      setForm(EMPTY);
      setPreviewCounts(null);
      qc.invalidateQueries({ queryKey: ["custom-push-recent"] });
    },
    onError: (e) => toast.error(asMessage(e)),
  });

  function canSubmit(): boolean {
    if (!form.title.trim()) return false;
    if (!form.body.trim()) return false;
    if (selectedClasses(form).length === 0) return false;
    return !sendMut.isPending && !previewMut.isPending;
  }

  function handleSend(e: React.FormEvent) {
    e.preventDefault();
    if (!canSubmit()) return;
    if (isProd) {
      setConfirmOpen(true);
    } else {
      sendMut.mutate();
    }
  }

  const pending = sendMut.isPending || previewMut.isPending;

  return (
    <div className="space-y-8">
      <PageHeader
        title="Custom push"
        description={
          isProd ? (
            <span>
              <Badge variant="warning" className="mr-2">
                production
              </Badge>
              Sends a real one-off notification. Confirm dialog required.
            </span>
          ) : (
            "Send a real one-off notification to a specific device class."
          )
        }
      />

      <GoingPushes items={recentQ.data ?? []} />

      <Card>
        <CardHeader>
          <div className="flex items-center gap-2">
            <Megaphone className="h-5 w-5 text-muted-foreground" />
            <CardTitle>New custom push</CardTitle>
          </div>
          <CardDescription>
            Title appears on the lock-screen; body is the alert text. With
            "Keep record" on, it also lands in the Announcement page.
          </CardDescription>
        </CardHeader>
        <CardContent>
          <form className="space-y-5" onSubmit={handleSend}>
            <div className="grid gap-1.5">
              <Label htmlFor="title">Title</Label>
              <Input
                id="title"
                value={form.title}
                onChange={(e) => setForm({ ...form, title: e.target.value })}
                maxLength={500}
                required
              />
            </div>
            <div className="grid gap-1.5">
              <Label htmlFor="body">Body</Label>
              <Textarea
                id="body"
                rows={4}
                value={form.body}
                onChange={(e) => setForm({ ...form, body: e.target.value })}
                maxLength={2000}
                required
              />
            </div>

            <div className="flex flex-wrap items-center gap-4">
              <label className="flex items-center gap-2 text-sm">
                <Checkbox
                  checked={form.keeps_record}
                  onCheckedChange={(v) =>
                    setForm({ ...form, keeps_record: v === true })
                  }
                />
                Keep record (appears in Announcement page)
              </label>
              <label className="flex items-center gap-2 text-sm">
                <Checkbox
                  checked={form.force_ring}
                  onCheckedChange={(v) =>
                    setForm({ ...form, force_ring: v === true })
                  }
                />
                Force ring (play sound)
              </label>
            </div>

            <div className="grid gap-1.5">
              <Label>Target classes</Label>
              <div className="flex flex-wrap gap-4 text-sm">
                <label className="flex items-center gap-2">
                  <Checkbox
                    checked={form.target_iphone}
                    onCheckedChange={(v) =>
                      setForm({ ...form, target_iphone: v === true })
                    }
                  />
                  iPhone
                </label>
                <label className="flex items-center gap-2">
                  <Checkbox
                    checked={form.target_ipad}
                    onCheckedChange={(v) =>
                      setForm({ ...form, target_ipad: v === true })
                    }
                  />
                  iPad
                </label>
                <label className="flex items-center gap-2">
                  <Checkbox
                    checked={form.target_android}
                    onCheckedChange={(v) =>
                      setForm({ ...form, target_android: v === true })
                    }
                  />
                  Android
                </label>
              </div>
            </div>

            <div>
              <Button
                type="button"
                variant="ghost"
                size="sm"
                onClick={() =>
                  setForm({ ...form, show_advanced: !form.show_advanced })
                }
              >
                {form.show_advanced ? (
                  <Minus className="mr-1 h-4 w-4" />
                ) : (
                  <Plus className="mr-1 h-4 w-4" />
                )}
                Advanced filters
              </Button>
              {form.show_advanced && (
                <div className="mt-3 grid gap-3 md:grid-cols-2">
                  <div className="grid gap-1.5">
                    <Label htmlFor="user_id">user_id (optional)</Label>
                    <Input
                      id="user_id"
                      value={form.user_id}
                      onChange={(e) =>
                        setForm({ ...form, user_id: e.target.value })
                      }
                      maxLength={64}
                    />
                  </div>
                  <div className="grid gap-1.5">
                    <Label htmlFor="device_id">device_id (optional)</Label>
                    <Input
                      id="device_id"
                      value={form.device_id}
                      onChange={(e) =>
                        setForm({ ...form, device_id: e.target.value })
                      }
                      maxLength={128}
                    />
                  </div>
                </div>
              )}
            </div>

            <div className="flex flex-wrap items-center gap-3">
              <Button
                type="button"
                variant="outline"
                onClick={() => previewMut.mutate()}
                disabled={pending || selectedClasses(form).length === 0}
              >
                Preview match count
              </Button>
              {previewCounts && (
                <span className="text-sm text-muted-foreground">
                  {Object.entries(previewCounts)
                    .filter(([k]) => k !== "total")
                    .map(([k, v]) => `${v} ${k}`)
                    .join(" · ")}{" "}
                  (total {previewCounts.total ?? 0})
                </span>
              )}
            </div>

            <div className="flex gap-2">
              <Button type="submit" disabled={!canSubmit()}>
                <Send className="mr-1 h-4 w-4" />
                {pending ? "Working…" : "Send"}
              </Button>
            </div>
          </form>
        </CardContent>
      </Card>

      <Section title="Recent sends (last 30)">
        {recentQ.isLoading && (
          <div className="text-sm text-muted-foreground">Loading…</div>
        )}
        {recentQ.data && recentQ.data.length === 0 && (
          <div className="text-sm text-muted-foreground">No sends yet.</div>
        )}
        {recentQ.data && recentQ.data.length > 0 && (
          <Table>
            <TableHeader>
              <TableRow>
                <TableHead>#</TableHead>
                <TableHead>Kind</TableHead>
                <TableHead>Title</TableHead>
                <TableHead>Targets</TableHead>
                <TableHead>Total</TableHead>
                <TableHead>Sent</TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {recentQ.data.map((r) => (
                <TableRow key={r.id}>
                  <TableCell className="font-mono text-xs">{r.id}</TableCell>
                  <TableCell>
                    <Badge
                      variant={r.kind === "record" ? "default" : "secondary"}
                      className="text-[10px] uppercase"
                    >
                      {r.kind}
                    </Badge>
                  </TableCell>
                  <TableCell className="max-w-md truncate">{r.title}</TableCell>
                  <TableCell className="text-xs">
                    {r.target_classes.join(" + ") || "—"}
                  </TableCell>
                  <TableCell className="tabular-nums">
                    {r.is_queueing ? (
                      <Badge variant="warning" className="text-[10px] uppercase">
                        queueing
                      </Badge>
                    ) : (
                      r.total
                    )}
                  </TableCell>
                  <TableCell className="text-xs text-muted-foreground">
                    {r.sent_at ? r.sent_at.replace("T", " ").slice(0, 16) : "—"}
                  </TableCell>
                </TableRow>
              ))}
            </TableBody>
          </Table>
        )}
      </Section>

      <Dialog open={confirmOpen} onOpenChange={setConfirmOpen}>
        <DialogContent>
          <DialogHeader>
            <DialogTitle>Send custom push in production?</DialogTitle>
            <DialogDescription>
              The server will fan out to every matched device. This is real
              and immediate.
              <br />
              <span className="font-mono text-xs">{form.title}</span>
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
                sendMut.mutate();
              }}
            >
              Send
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </div>
  );
}

function asMessage(e: unknown): string {
  if (e instanceof ApiError) return `HTTP ${e.status} — ${e.message}`;
  if (e instanceof Error) return e.message;
  return String(e);
}

function GoingPushes({ items }: { items: CustomPushRecentItem[] }) {
  const going = items.filter((r) => r.is_queueing);
  if (going.length === 0) return null;
  return (
    <Card>
      <CardHeader className="pb-3">
        <div className="flex items-center gap-2">
          <Loader2 className="h-5 w-5 animate-spin text-muted-foreground" />
          <CardTitle>Going pushes</CardTitle>
        </div>
        <CardDescription>
          Queued sends the dispatcher hasn't fanned out yet. Refreshes every 2s.
        </CardDescription>
      </CardHeader>
      <CardContent>
        <Table>
          <TableHeader>
            <TableRow>
              <TableHead>#</TableHead>
              <TableHead>Kind</TableHead>
              <TableHead>Title</TableHead>
              <TableHead>Targets</TableHead>
              <TableHead>Status</TableHead>
            </TableRow>
          </TableHeader>
          <TableBody>
            {going.map((r) => (
              <TableRow key={r.id}>
                <TableCell className="font-mono text-xs">{r.id}</TableCell>
                <TableCell>
                  <Badge
                    variant={r.kind === "record" ? "default" : "secondary"}
                    className="text-[10px] uppercase"
                  >
                    {r.kind}
                  </Badge>
                </TableCell>
                <TableCell className="max-w-md truncate">{r.title}</TableCell>
                <TableCell className="text-xs">
                  {r.target_classes.join(" + ") || "—"}
                </TableCell>
                <TableCell>
                  <Badge variant="warning" className="text-[10px] uppercase">
                    queueing
                  </Badge>
                </TableCell>
              </TableRow>
            ))}
          </TableBody>
        </Table>
      </CardContent>
    </Card>
  );
}
