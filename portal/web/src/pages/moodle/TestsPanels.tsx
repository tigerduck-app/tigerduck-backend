// The Tests sections of the sync topology: sends that exist to exercise one
// push path end to end against a real device, instead of waiting for the
// server to decide to send it. Each goes through the real push pipeline; only
// the trigger is manual. The routes live in portal/app/routes/moodle/manual_tests.py.

import { useState, type ReactNode } from "react";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { fmt } from "./format";
import type { SyncDevice } from "./types";

type PostResult =
  | { ok: true; data: Record<string, unknown> }
  | { ok: false; error: string };

/**
 * POST and hand back the server's reason on failure, from whichever field it
 * came in: our routes answer `error`, a FastAPI validation failure `detail`,
 * and a proxy in front of the portal plain text.
 */
export async function postTest(url: string, body?: unknown): Promise<PostResult> {
  try {
    const res = await fetch(url, {
      method: "POST",
      headers: body === undefined ? undefined : { "Content-Type": "application/json" },
      body: body === undefined ? undefined : JSON.stringify(body),
    });
    const text = await res.text();
    let data: Record<string, unknown>;
    try {
      data = JSON.parse(text);
    } catch {
      data = { error: text.slice(0, 300) || `HTTP ${res.status}` };
    }
    if (!res.ok || data.ok === false) {
      const reason = data.error ?? data.detail ?? `HTTP ${res.status}`;
      return { ok: false, error: typeof reason === "string" ? reason : JSON.stringify(reason) };
    }
    return { ok: true, data };
  } catch (e) {
    return { ok: false, error: e instanceof Error ? e.message : String(e) };
  }
}

type Outcome = { ok: boolean; text: string } | null;

function OutcomeLine({ outcome }: { outcome: Outcome }) {
  if (!outcome) return null;
  return (
    <p className={`text-xs ${outcome.ok ? "text-muted-foreground" : "text-destructive"}`}>
      {outcome.text}
    </p>
  );
}

function TestCard({
  title,
  description,
  children,
}: {
  title: string;
  description: string;
  children: ReactNode;
}) {
  return (
    <div className="rounded-md border border-border p-3 space-y-3">
      <div>
        <div className="text-sm font-medium">{title}</div>
        <p className="text-xs text-muted-foreground mt-1">{description}</p>
      </div>
      {children}
    </div>
  );
}

/** Backend › Tests. */
export function BackendTests({ studentId }: { studentId: string }) {
  const [busy, setBusy] = useState(false);
  const [outcome, setOutcome] = useState<Outcome>(null);

  return (
    <div className="space-y-3 py-2">
      <TestCard
        title="Moodle token expired notice"
        description="Queues the notice the server sends when Moodle rejects this account's stored token, to every device on the account that holds a push token (a Mac takes none), in each device's own language. Only the notice is sent: the account's Moodle sign-in and its sync jobs are left as they are."
      >
        <Button
          size="sm"
          variant="outline"
          disabled={busy}
          onClick={async () => {
            setBusy(true);
            const r = await postTest(
              `/api/moodle/tests/reauth-push?student_id=${encodeURIComponent(studentId)}`
            );
            setBusy(false);
            if (!r.ok) {
              setOutcome({ ok: false, text: `Failed: ${r.error}` });
              return;
            }
            const devices = Number(r.data.devices ?? 0);
            setOutcome({
              ok: true,
              text: `Queued as job #${r.data.push_job_id} for ${devices} device${devices === 1 ? "" : "s"}.`,
            });
          }}
        >
          Send test Moodle token expire notification
        </Button>
        <OutcomeLine outcome={outcome} />
      </TestCard>
    </div>
  );
}

const SCENARIOS: { value: string; label: string }[] = [
  { value: "inClass", label: "In class (上課)" },
  { value: "classPreparing", label: "Class starting soon (即將上課)" },
  { value: "assignmentUrgent", label: "Assignment due (作業)" },
];

/** An iPhone or iPad's Tests tab. */
export function LiveActivityTest({ studentId, device }: { studentId: string; device: SyncDevice }) {
  const [scenario, setScenario] = useState("inClass");
  const [title, setTitle] = useState("Portal test");
  const [subtitle, setSubtitle] = useState("Live Activity test");
  const [location, setLocation] = useState("");
  const [minutes, setMinutes] = useState("2");
  const [busy, setBusy] = useState(false);
  const [outcome, setOutcome] = useState<Outcome>(null);

  const minutesValue = Number(minutes);
  const valid =
    title.trim().length > 0 && Number.isInteger(minutesValue) && minutesValue >= 1 && minutesValue <= 240;

  return (
    <div className="space-y-3 py-2">
      <TestCard
        title="Live Activity"
        description="Starts a Live Activity on this device through the server's push-to-start path. The phone then registers it, the server files its end for when the countdown runs out, and that end push takes it down. Its end shows under Queued Jobs once the phone has registered it, where End now dismisses it early. The fake clock has to be off on the phone — it shifts every date this sends — and Live Updates and TigerSync on, or the phone ends it on arrival."
      >
        <div className="grid gap-3 sm:grid-cols-2">
          <div className="space-y-1">
            <Label className="text-xs">Scenario</Label>
            <Select value={scenario} onValueChange={setScenario}>
              <SelectTrigger className="h-8 text-xs">
                <SelectValue />
              </SelectTrigger>
              <SelectContent>
                {SCENARIOS.map((s) => (
                  <SelectItem key={s.value} value={s.value}>{s.label}</SelectItem>
                ))}
              </SelectContent>
            </Select>
          </div>
          <div className="space-y-1">
            <Label htmlFor="la-test-minutes" className="text-xs">Countdown (minutes)</Label>
            <Input
              id="la-test-minutes"
              type="number"
              min={1}
              max={240}
              value={minutes}
              onChange={(e) => setMinutes(e.target.value)}
              className="h-8 text-xs"
            />
          </div>
          <div className="space-y-1">
            <Label htmlFor="la-test-title" className="text-xs">Title</Label>
            <Input
              id="la-test-title"
              value={title}
              maxLength={120}
              onChange={(e) => setTitle(e.target.value)}
              className="h-8 text-xs"
            />
          </div>
          <div className="space-y-1">
            <Label htmlFor="la-test-subtitle" className="text-xs">Subtitle</Label>
            <Input
              id="la-test-subtitle"
              value={subtitle}
              maxLength={120}
              onChange={(e) => setSubtitle(e.target.value)}
              className="h-8 text-xs"
            />
          </div>
          <div className="space-y-1">
            <Label htmlFor="la-test-location" className="text-xs">Location (optional)</Label>
            <Input
              id="la-test-location"
              value={location}
              maxLength={60}
              placeholder="TR-313"
              onChange={(e) => setLocation(e.target.value)}
              className="h-8 text-xs"
            />
          </div>
        </div>
        <Button
          size="sm"
          disabled={busy || !valid}
          onClick={async () => {
            setBusy(true);
            const r = await postTest("/api/moodle/tests/live-activity", {
              student_id: studentId,
              device_id: device.id,
              scenario,
              title: title.trim(),
              subtitle: subtitle.trim(),
              location: location.trim(),
              minutes: minutesValue,
            });
            setBusy(false);
            if (!r.ok) {
              setOutcome({ ok: false, text: `Failed: ${r.error}` });
              return;
            }
            setOutcome({
              ok: true,
              text: `Started as job #${r.data.push_job_id} (${r.data.activity_id}); its countdown ends ${fmt(String(r.data.ends_at))}.`,
            });
          }}
        >
          Start Live Activity
        </Button>
        <OutcomeLine outcome={outcome} />
      </TestCard>
    </div>
  );
}

/** Queued Jobs' button on a Live Activity end: fire it now rather than at its countdown target. */
export function EndNowButton({ studentId, jobId }: { studentId: string; jobId: number }) {
  const [busy, setBusy] = useState(false);
  return (
    <Button
      size="sm"
      variant="outline"
      className="h-6 px-2 text-[10px] mt-1"
      disabled={busy}
      onClick={async () => {
        setBusy(true);
        const r = await postTest(
          `/api/moodle/tests/live-activity-end-now?student_id=${encodeURIComponent(studentId)}&job_id=${jobId}`
        );
        // On success the row leaves the queue on the next poll; the button
        // stays disabled until then so it cannot be pressed twice.
        if (!r.ok) {
          setBusy(false);
          alert("End now failed: " + r.error);
        }
      }}
    >
      {busy ? "Ending…" : "End now"}
    </Button>
  );
}
