import { useState } from "react";
import { Check, CircleAlert, RefreshCw, X } from "lucide-react";
import { Button } from "@/components/ui/button";
import { FormDialog, fieldClass } from "@/components/page";
import { cn } from "@/lib/cn";
import { fullDate } from "@/lib/format";

export type PinDialogKind = "pin" | "hosts" | "expiry" | "remove";

export type PinDialogResult =
  | { kind: "pin"; spki: string; label: string }
  | { kind: "hosts"; hosts: string[] }
  | { kind: "expiry"; date: string }
  | { kind: "remove"; spki: string };

const SPKI = /^[A-Za-z0-9+/]{43}=$/;
// A dotted name or IPv4 address with an optional port; the backend has the final say.
const HOST = /^([a-z0-9-]+\.)+[a-z0-9-]+(:\d{1,5})?$/i;

const TITLE = { pin: "Add pin", hosts: "Monitored hosts", expiry: "Extend expiry", remove: "Remove pin?" };
const ACTION = { pin: "Add and publish", hosts: "Save", expiry: "Save and publish", remove: "Remove and publish" };

// The expiry the page sends for a picked day: that day at 00:00 UTC.
function instant(day: string): number {
  return Date.parse(`${day}T00:00:00Z`);
}

function daysFromNow(day: string): number {
  return Math.round((instant(day) - Date.now()) / 86_400_000);
}

export function TlsPinDialog({
  kind,
  setId,
  hosts: initialHosts,
  expiresAt,
  pin,
  pending,
  onClose,
  onSave,
}: {
  kind: PinDialogKind;
  setId: string;
  hosts: string[];
  /** The set's current expiry, ISO 8601. */
  expiresAt: string;
  pin?: { label: string; spki: string };
  pending: boolean;
  onClose: () => void;
  onSave: (result: PinDialogResult) => void;
}) {
  const [spki, setSpki] = useState("");
  const [label, setLabel] = useState("");
  const [hosts, setHosts] = useState(initialHosts.join("\n"));
  const expires = expiresAt.slice(0, 10);
  const [date, setDate] = useState(expires);

  const key = spki.trim();
  const spkiOk = SPKI.test(key);
  const lines = hosts.split("\n").map((h) => h.trim()).filter(Boolean);
  const badHosts = lines.filter((h) => !HOST.test(h)).length;
  const invalid =
    kind === "pin"
      ? !(spkiOk && label.trim())
      : kind === "hosts"
        ? badHosts > 0
        : kind === "expiry"
          ? // The backend takes only an expiry later than the current one.
            !(instant(date) > Date.now() && instant(date) > Date.parse(expiresAt))
          : false;

  const save = () => {
    if (kind === "pin") onSave({ kind, spki: key, label: label.trim() });
    else if (kind === "hosts") onSave({ kind, hosts: lines });
    else if (kind === "expiry") onSave({ kind, date });
    else if (pin) onSave({ kind, spki: pin.spki });
  };

  const oldDays = daysFromNow(expires);
  const newDays = date ? daysFromNow(date) : oldDays;
  const span = Math.max(newDays, oldDays, 1);
  const pct = (d: number) => `${Math.round((Math.max(d, 0) / span) * 100)}%`;

  return (
    <FormDialog
      open
      onOpenChange={(open) => !open && onClose()}
      title={TITLE[kind]}
      meta={setId}
      width={kind === "hosts" ? 600 : 560}
      footer={
        <>
          <Button variant="line" size="form" onClick={onClose}>
            Cancel
          </Button>
          <Button
            variant={kind === "remove" ? "danger" : "ink"}
            size="form"
            disabled={invalid || pending}
            onClick={save}
          >
            {ACTION[kind]}
          </Button>
        </>
      }
    >
      {kind === "pin" && (
        <>
          <div className="flex flex-col gap-1.5">
            <label htmlFor="pin-spki" className="text-[13px] font-medium">
              SPKI SHA-256 (base64)
            </label>
            <input
              id="pin-spki"
              value={spki}
              onChange={(e) => setSpki(e.target.value)}
              placeholder="44 characters ending in ="
              spellCheck={false}
              autoFocus
              className={cn(fieldClass, "font-mono text-[13px]", key && !spkiOk && "border-dot-bad")}
            />
            <span
              className={cn(
                "flex items-center gap-1.5 text-xs tabular-nums",
                spkiOk ? "text-badge-success-fg" : key ? "text-badge-danger-fg" : "text-ink-3",
              )}
            >
              {spkiOk ? <Check className="h-3.5 w-3.5" /> : <CircleAlert className="h-3.5 w-3.5" />}
              {spkiOk ? "Valid SHA-256 pin" : `${key.length} / 44 characters`}
            </span>
          </div>
          <label className="flex flex-col gap-1.5 text-[13px] font-medium">
            Label
            <input
              value={label}
              onChange={(e) => setLabel(e.target.value)}
              placeholder="TWCA Secure SSL CA"
              className={cn(fieldClass, "font-normal")}
            />
          </label>
        </>
      )}

      {kind === "hosts" && (
        <>
          <div className="flex flex-col gap-1.5">
            <label htmlFor="pin-hosts" className="text-[13px] font-medium">
              Hosts <span className="font-normal text-ink-3">· one host or host:port per line</span>
            </label>
            <textarea
              id="pin-hosts"
              rows={6}
              value={hosts}
              onChange={(e) => setHosts(e.target.value)}
              spellCheck={false}
              autoFocus
              className="w-full resize-y rounded-[9px] border border-field bg-surface p-2.5 font-mono text-[13px] leading-relaxed text-ink"
            />
          </div>
          <div className="flex flex-wrap gap-1.5">
            {lines.map((h, i) => {
              const ok = HOST.test(h);
              return (
                <span
                  key={`${h}-${i}`}
                  className={cn(
                    "inline-flex min-h-7 items-center gap-1 break-all rounded-full px-2.5 font-mono text-xs",
                    ok ? "bg-badge-info text-badge-info-fg" : "bg-badge-danger text-badge-danger-fg",
                  )}
                >
                  {ok ? <Check className="h-3 w-3 shrink-0" /> : <X className="h-3 w-3 shrink-0" />}
                  {h}
                </span>
              );
            })}
          </div>
        </>
      )}

      {kind === "expiry" && (
        <>
          <label className="flex max-w-[240px] flex-col gap-1.5 text-[13px] font-medium">
            New expiry (UTC)
            <input
              type="date"
              value={date}
              onChange={(e) => setDate(e.target.value)}
              className={cn(fieldClass, "font-normal")}
            />
          </label>
          <div className="flex flex-col gap-2 rounded-xl bg-ground px-4 py-3.5">
            <div aria-hidden="true" className="relative h-2.5 rounded-full bg-line">
              <div className="absolute left-0 top-0 h-2.5 rounded-full bg-dot-off" style={{ width: pct(oldDays) }} />
              <div className="absolute left-0 top-[3px] h-1 rounded-full bg-chart-1" style={{ width: pct(newDays) }} />
            </div>
            <div className="flex flex-wrap justify-between gap-2 text-xs tabular-nums text-ink-2">
              <span className="flex items-center gap-1.5">
                <span aria-hidden="true" className="size-2.5 rounded-[3px] bg-dot-off" />
                Now {fullDate(expires, true)} · {oldDays} d
              </span>
              <span className="flex items-center gap-1.5">
                <span aria-hidden="true" className="size-2.5 rounded-[3px] bg-chart-1" />
                New {date ? `${fullDate(date, true)} · ${newDays} d` : "—"}
              </span>
            </div>
          </div>
        </>
      )}

      {kind === "remove" && pin && (
        <>
          <div className="flex flex-col gap-1.5 rounded-xl border border-line px-4 py-3.5">
            <span className="break-words font-semibold">{pin.label}</span>
            <span className="break-all font-mono text-xs text-ink-2">{pin.spki}</span>
          </div>
          <div className="flex items-center gap-2 text-[13px] text-ink-2">
            <RefreshCw className="h-4 w-4 shrink-0 text-ink-3" />
            Publishes a new signed document
          </div>
        </>
      )}
    </FormDialog>
  );
}
