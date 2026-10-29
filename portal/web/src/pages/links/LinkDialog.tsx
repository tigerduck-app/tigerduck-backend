import { useState } from "react";
import { Button } from "@/components/ui/button";
import { ErrorAlert, FormDialog, fieldClass } from "@/components/page";
import { cn } from "@/lib/cn";
import type { LinkRow } from "@/types/api";

export type LinkBody = {
  name: string;
  target_url: string;
  check_interval_minutes: number;
  expectations: unknown[];
};

const RULES_EXAMPLE = `[
  {"type": "status", "equals": 200},
  {"type": "final_host", "in": ["www.academic.ntust.edu.tw"]},
  {"type": "href_suffix_count", "suffix": ".ics", "min": 1}
]`;

const PRESETS: [number, string][] = [
  [15, "15 min"],
  [60, "1 h"],
  [360, "6 h"],
  [1440, "1 day"],
];

// The checker's rule types as chips, so a wrong field name stands out before saving.
function chip(rule: unknown): { type: string; value: string } {
  if (!rule || typeof rule !== "object") return { type: "invalid", value: String(rule) };
  const r = rule as Record<string, unknown>;
  if (r.type === "status") return { type: "status", value: `= ${r.equals}` };
  if (r.type === "final_host")
    return { type: "final host", value: Array.isArray(r.in) ? r.in.join(", ") : String(r.in) };
  if (r.type === "href_suffix_count") return { type: "links ending", value: `${r.suffix} ≥ ${r.min}` };
  if (r.type === "contains") return { type: "contains", value: String(r.text) };
  return { type: String(r.type), value: JSON.stringify(rule) };
}

function parseRules(text: string): unknown[] | null {
  try {
    const parsed: unknown = JSON.parse(text || "[]");
    return Array.isArray(parsed) ? parsed : null;
  } catch {
    return null;
  }
}

/** New link when `link` is null, otherwise edits it. */
export function LinkDialog({
  link,
  error,
  pending,
  onClose,
  onSave,
}: {
  link: LinkRow | null;
  error: string | null;
  pending: boolean;
  onClose: () => void;
  onSave: (body: LinkBody) => void;
}) {
  const [name, setName] = useState(link?.name ?? "");
  const [target, setTarget] = useState(link?.target_url ?? "");
  const [minutes, setMinutes] = useState(String(link?.check_interval_minutes ?? 60));
  const [rules, setRules] = useState(link ? JSON.stringify(link.expectations, null, 2) : "");
  const [jsonError, setJsonError] = useState<string | null>(null);

  const isNew = link === null;
  const parsed = parseRules(rules);
  const disabled = pending || !target.trim() || (isNew && !name.trim());

  const save = () => {
    if (!parsed) {
      setJsonError("Rules must be a JSON array");
      return;
    }
    onSave({
      name: name.trim(),
      target_url: target.trim(),
      check_interval_minutes: Number(minutes),
      expectations: parsed,
    });
  };

  return (
    <FormDialog
      open
      onOpenChange={(open) => !open && onClose()}
      title={isNew ? "New link" : `Edit ${link.name}`}
      meta={<span className="font-mono">/links/{name || "name"}</span>}
      footer={
        <>
          <Button variant="line" size="form" onClick={onClose}>
            Cancel
          </Button>
          <Button variant="ink" size="form" disabled={disabled} onClick={save}>
            {isNew ? "Create" : "Save"}
          </Button>
        </>
      }
    >
      {error && <ErrorAlert>{error}</ErrorAlert>}

      {isNew && (
        <label className="flex flex-col gap-1.5 text-[13px] font-medium">
          Name
          <input
            value={name}
            onChange={(e) => setName(e.target.value.toLowerCase())}
            placeholder="ntust-calendar"
            maxLength={64}
            autoFocus
            className={cn(fieldClass, "font-mono font-normal")}
          />
        </label>
      )}

      <label className="flex flex-col gap-1.5 text-[13px] font-medium">
        Target URL
        <input
          type="url"
          value={target}
          onChange={(e) => setTarget(e.target.value)}
          placeholder="https://www.academic.ntust.edu.tw/…"
          maxLength={2048}
          className={cn(fieldClass, "font-normal")}
        />
      </label>

      <div className="flex flex-col gap-1.5">
        <span id="link-every" className="text-[13px] font-medium">
          Check every
        </span>
        <div role="group" aria-labelledby="link-every" className="flex flex-wrap items-center gap-1.5">
          {PRESETS.map(([m, label]) => {
            const on = String(m) === minutes;
            return (
              <button
                key={m}
                type="button"
                aria-pressed={on}
                onClick={() => setMinutes(String(m))}
                className={cn(
                  "min-h-[34px] rounded-full border px-3 text-[13px]",
                  on ? "border-ink bg-ink text-on-ink" : "border-field bg-surface text-ink hover:bg-row-hover",
                )}
              >
                {label}
              </button>
            );
          })}
          <label className="flex items-center gap-1.5 text-[13px] text-ink-2">
            <input
              type="number"
              min={5}
              max={1440}
              value={minutes}
              onChange={(e) => setMinutes(e.target.value)}
              aria-label="Minutes"
              className={cn(fieldClass, "h-[34px] w-[76px] px-2 tabular-nums")}
            />
            min
          </label>
        </div>
      </div>

      <div className="flex flex-col gap-1.5">
        <label htmlFor="link-rules" className="text-[13px] font-medium">
          Rules
        </label>
        <textarea
          id="link-rules"
          rows={6}
          value={rules}
          onChange={(e) => {
            setRules(e.target.value);
            setJsonError(null);
          }}
          placeholder={RULES_EXAMPLE}
          spellCheck={false}
          className={cn(
            "w-full resize-y rounded-[9px] border bg-surface p-2.5 font-mono text-xs leading-normal text-ink placeholder:text-ink-3",
            jsonError ? "border-dot-bad" : "border-field",
          )}
        />
        {jsonError && <span className="text-xs text-badge-danger-fg">{jsonError}</span>}
        {parsed && parsed.length > 0 && (
          <div className="flex flex-wrap gap-1.5 pt-0.5">
            {parsed.map((rule, i) => {
              const c = chip(rule);
              return (
                <span
                  key={i}
                  className="inline-flex min-h-7 items-center gap-1.5 rounded-full bg-badge-info px-2.5 text-xs text-badge-info-fg"
                >
                  <span className="font-semibold">{c.type}</span>
                  <span className="break-all font-mono">{c.value}</span>
                </span>
              );
            })}
          </div>
        )}
      </div>
    </FormDialog>
  );
}
