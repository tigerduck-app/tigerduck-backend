import { useState } from "react";
import { useMutation, useQuery } from "@tanstack/react-query";
import { Link } from "react-router-dom";
import { toast } from "sonner";
import { Send } from "lucide-react";
import { api } from "@/lib/api";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { RadioGroup, RadioGroupItem } from "@/components/ui/radio-group";
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
import type { DeviceInfo } from "@/types/api";

type Kind = "alert" | "classPreparing" | "inClass" | "assignmentUrgent" | "end";

const KINDS: { value: Kind; label: string }[] = [
  { value: "alert", label: "Alert" },
  { value: "classPreparing", label: "LA: classPreparing" },
  { value: "inClass", label: "LA: inClass" },
  { value: "assignmentUrgent", label: "LA: assignmentUrgent" },
  { value: "end", label: "End LA" },
];

export function AppleTestPushPage() {
  const devicesQ = useQuery<DeviceInfo[]>({
    queryKey: ["test-devices"],
    queryFn: () => api<DeviceInfo[]>("/api/test/devices"),
  });

  const [selectedDevice, setSelectedDevice] = useState<string | null>(null);
  const [kind, setKind] = useState<Kind>("alert");

  // Auto-select first device once devices load (matches the old default).
  if (
    selectedDevice === null &&
    devicesQ.data &&
    devicesQ.data.length > 0
  ) {
    setSelectedDevice(devicesQ.data[0].device_id);
  }

  return (
    <div className="space-y-8">
      <PageHeader
        title="Apple test push"
        description={
          <>
            Fires synthetic pushes through{" "}
            <code>/v2/_debug/*</code> — no DB writes. For a real
            DB-backed announcement, use{" "}
            <Link
              to="/announcement"
              className="text-primary underline-offset-4 hover:underline"
            >
              Announcement
            </Link>
            .
          </>
        }
      />

      <Section title="Devices">
        {devicesQ.isLoading && (
          <div className="text-sm text-muted-foreground">Loading…</div>
        )}
        {devicesQ.isError && (
          <Card>
            <CardContent className="text-sm text-destructive">
              Could not load devices: {(devicesQ.error as Error).message}
            </CardContent>
          </Card>
        )}
        {devicesQ.data && devicesQ.data.length === 0 && (
          <Card>
            <CardContent className="text-sm text-muted-foreground">
              No registered Apple devices. Enable Server push in the iOS
              app first.
            </CardContent>
          </Card>
        )}
        {devicesQ.data && devicesQ.data.length > 0 && (
          <Table>
            <TableHeader>
              <TableRow>
                <TableHead className="w-8"></TableHead>
                <TableHead>device_id</TableHead>
                <TableHead>user_id</TableHead>
                <TableHead>bundle</TableHead>
                <TableHead>apns_env</TableHead>
                <TableHead title="Push-to-Start token">PTS</TableHead>
                <TableHead title="Alert push token">alert</TableHead>
                <TableHead title="Active Live Activities">LA</TableHead>
                <TableHead>last update</TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {devicesQ.data.map((d) => (
                <TableRow
                  key={d.device_id}
                  data-state={
                    selectedDevice === d.device_id ? "selected" : undefined
                  }
                >
                  <TableCell>
                    <input
                      type="radio"
                      name="device"
                      checked={selectedDevice === d.device_id}
                      onChange={() => setSelectedDevice(d.device_id)}
                    />
                  </TableCell>
                  <TableCell className="max-w-[12rem] truncate font-mono text-xs">
                    {d.device_id}
                  </TableCell>
                  <TableCell className="text-sm">{d.user_id}</TableCell>
                  <TableCell className="font-mono text-xs">
                    {d.bundle_id}
                  </TableCell>
                  <TableCell>
                    <Badge
                      variant={
                        d.apns_env === "production"
                          ? "warning"
                          : d.apns_env === "development"
                            ? "success"
                            : "muted"
                      }
                    >
                      {d.apns_env || "?"}
                    </Badge>
                  </TableCell>
                  <TableCell>{d.has_pts_token ? "✓" : "—"}</TableCell>
                  <TableCell>{d.has_device_token ? "✓" : "—"}</TableCell>
                  <TableCell className="tabular-nums">
                    {d.active_live_activities}
                  </TableCell>
                  <TableCell className="text-xs text-muted-foreground">
                    {d.updated_at}
                  </TableCell>
                </TableRow>
              ))}
            </TableBody>
          </Table>
        )}
      </Section>

      <Section title="Send">
        <Card>
          <CardHeader>
            <CardTitle>Push type</CardTitle>
            <CardDescription>
              Alert goes to one device; Live Activity scenarios fire the
              backend's debug LA flows; End cancels by activity id.
            </CardDescription>
          </CardHeader>
          <CardContent className="space-y-6">
            <RadioGroup
              value={kind}
              onValueChange={(v) => setKind(v as Kind)}
              className="grid grid-cols-2 gap-2 sm:grid-cols-3"
            >
              {KINDS.map((k) => (
                <label
                  key={k.value}
                  className="flex items-center gap-2 rounded-md border border-border px-3 py-2 text-sm hover:bg-accent"
                >
                  <RadioGroupItem value={k.value} id={`kind-${k.value}`} />
                  <span>{k.label}</span>
                </label>
              ))}
            </RadioGroup>

            {kind === "alert" && (
              <AlertForm deviceId={selectedDevice} />
            )}
            {kind === "end" && <EndForm />}
            {(kind === "classPreparing" ||
              kind === "inClass" ||
              kind === "assignmentUrgent") && (
              <LiveActivityForm deviceId={selectedDevice} scenario={kind} />
            )}
          </CardContent>
        </Card>
      </Section>
    </div>
  );
}

function ResultBlock({ result }: { result: SendResult | null }) {
  if (!result) return null;
  const ok = result.status >= 200 && result.status < 300;
  return (
    <pre
      className={
        "max-h-72 overflow-auto rounded-md border p-3 font-mono text-xs " +
        (ok
          ? "border-success/40 bg-success/5"
          : "border-destructive/40 bg-destructive/10 text-destructive")
      }
    >
      {"HTTP " + result.status + "\n" + JSON.stringify(result.body, null, 2)}
    </pre>
  );
}

type SendResult = { status: number; body: unknown };

function useSend(path: string) {
  return useMutation<SendResult, Error, unknown>({
    mutationFn: async (json) => {
      const res = await fetch(path, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(json),
      });
      let body: unknown;
      try {
        body = await res.json();
      } catch {
        body = { raw: await res.text() };
      }
      return { status: res.status, body };
    },
    onSuccess: (r) => {
      const ok = r.status >= 200 && r.status < 300;
      if (ok) toast.success(`HTTP ${r.status}`);
      else toast.error(`HTTP ${r.status}`);
    },
    onError: (e) => toast.error(e.message),
  });
}

function AlertForm({ deviceId }: { deviceId: string | null }) {
  const [title, setTitle] = useState("Test alert");
  const [body, setBody] = useState("Synthetic alert push.");
  const send = useSend("/api/test/send_alert");

  return (
    <form
      className="space-y-3"
      onSubmit={(e) => {
        e.preventDefault();
        if (!deviceId) {
          toast.error("Pick a device first.");
          return;
        }
        send.mutate({ title, body, device_ids: [deviceId] });
      }}
    >
      <div className="grid gap-1.5">
        <Label htmlFor="alert-title">Title</Label>
        <Input
          id="alert-title"
          value={title}
          onChange={(e) => setTitle(e.target.value)}
        />
      </div>
      <div className="grid gap-1.5">
        <Label htmlFor="alert-body">Body</Label>
        <Textarea
          id="alert-body"
          rows={3}
          value={body}
          onChange={(e) => setBody(e.target.value)}
        />
      </div>
      <Button type="submit" disabled={send.isPending}>
        <Send className="mr-1 h-4 w-4" />
        {send.isPending ? "Sending…" : "Send"}
      </Button>
      <ResultBlock result={send.data ?? null} />
    </form>
  );
}

function LiveActivityForm({
  deviceId,
  scenario,
}: {
  deviceId: string | null;
  scenario: string;
}) {
  const [title, setTitle] = useState("Algorithms");
  const [subtitle, setSubtitle] = useState("Section A");
  const [location, setLocation] = useState("T2-401");
  const [countdown, setCountdown] = useState("");
  const [sourceId, setSourceId] = useState("debug-test");
  const send = useSend("/api/test/send_live_activity");

  return (
    <form
      className="space-y-3"
      onSubmit={(e) => {
        e.preventDefault();
        if (!deviceId) {
          toast.error("Pick a device first.");
          return;
        }
        send.mutate({
          device_id: deviceId,
          scenario,
          title,
          subtitle,
          location_text: location,
          countdown_target_iso: countdown.trim() || null,
          source_id: sourceId || "debug-test",
        });
      }}
    >
      <div className="grid gap-3 sm:grid-cols-2">
        <Field label="Title" value={title} onChange={setTitle} />
        <Field label="Subtitle" value={subtitle} onChange={setSubtitle} />
        <Field label="Location" value={location} onChange={setLocation} />
        <Field
          label="Countdown target (ISO8601, optional)"
          placeholder="2026-05-23T18:30:00Z"
          value={countdown}
          onChange={setCountdown}
        />
        <Field label="Source id" value={sourceId} onChange={setSourceId} />
      </div>
      <Button type="submit" disabled={send.isPending}>
        <Send className="mr-1 h-4 w-4" />
        {send.isPending ? "Sending…" : "Send"}
      </Button>
      <ResultBlock result={send.data ?? null} />
    </form>
  );
}

function EndForm() {
  const [activityId, setActivityId] = useState("");
  const send = useSend("/api/test/end_live_activity");

  return (
    <form
      className="space-y-3"
      onSubmit={(e) => {
        e.preventDefault();
        const aid = activityId.trim();
        if (!aid) {
          toast.error("Enter an activity_id.");
          return;
        }
        send.mutate({ activity_id: aid });
      }}
    >
      <div className="grid gap-1.5">
        <Label htmlFor="aid">Activity id</Label>
        <Input
          id="aid"
          placeholder="classPreparing::COURSE-123"
          value={activityId}
          onChange={(e) => setActivityId(e.target.value)}
        />
        <p className="text-xs text-muted-foreground">
          Format: <code>scenario::source_id</code>. Same value{" "}
          <code>send_live_activity</code> returned.
        </p>
      </div>
      <Button type="submit" disabled={send.isPending}>
        <Send className="mr-1 h-4 w-4" />
        {send.isPending ? "Sending…" : "End"}
      </Button>
      <ResultBlock result={send.data ?? null} />
    </form>
  );
}

function Field({
  label,
  value,
  onChange,
  placeholder,
}: {
  label: string;
  value: string;
  onChange: (v: string) => void;
  placeholder?: string;
}) {
  return (
    <div className="grid gap-1.5">
      <Label>{label}</Label>
      <Input
        value={value}
        placeholder={placeholder}
        onChange={(e) => onChange(e.target.value)}
      />
    </div>
  );
}
