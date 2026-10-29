// Adds devices to a list, found by search or pasted as device IDs.

import { useState } from "react";
import { keepPreviousData, useQuery } from "@tanstack/react-query";
import { Check, Search, UserPlus, X } from "lucide-react";
import { api } from "@/lib/api";
import { cn } from "@/lib/cn";
import { Button } from "@/components/ui/button";
import { DialogIcon, ErrorAlert, FormDialog } from "@/components/page";
import type { DevicesPayload } from "@/types/api";
import { classLabel } from "@/pages/devices/format";
import { errorText } from "./format";

const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;
const plural = (n: number) => `${n} ${n === 1 ? "device" : "devices"}`;

export function AddMembersDialog({
  memberIds,
  pending,
  error,
  onClose,
  onAdd,
}: {
  memberIds: ReadonlySet<string>;
  pending: boolean;
  error: unknown;
  onClose: () => void;
  onAdd: (deviceIds: string[]) => void;
}) {
  const [tab, setTab] = useState<"search" | "paste">("search");
  const [query, setQuery] = useState("");
  const [picked, setPicked] = useState<string[]>([]);
  const [pasted, setPasted] = useState("");

  const term = query.trim();
  const searchQ = useQuery<DevicesPayload>({
    queryKey: ["device-search", term],
    queryFn: () =>
      api<DevicesPayload>(`/api/devices?limit=100&search=${encodeURIComponent(term)}`),
    enabled: term.length >= 2,
    placeholderData: keepPreviousData,
  });
  const matches = term.length >= 2 ? (searchQ.data?.items ?? []) : [];
  const results = matches.filter((d) => !memberIds.has(d.device_id));
  const hidden = matches.length - results.length;
  const chosen = picked.filter((id) => results.some((d) => d.device_id === id));
  const allOn = results.length > 0 && chosen.length === results.length;

  const parts = pasted.split(/[\s,]+/).filter(Boolean);
  const valid = parts.filter((s) => UUID.test(s));
  const invalid = parts.filter((s) => !UUID.test(s));

  const ids = tab === "search" ? chosen : valid;
  const toggle = (id: string) =>
    setPicked((p) => (p.includes(id) ? p.filter((x) => x !== id) : [...p, id]));
  const tabClass = (on: boolean) =>
    cn(
      "-mb-px min-h-10 border-b-2 px-3 text-sm",
      on ? "border-ink font-semibold text-ink" : "border-transparent text-ink-3 hover:text-ink",
    );

  return (
    <FormDialog
      open
      onOpenChange={(open) => !open && onClose()}
      width={760}
      icon={<DialogIcon icon={UserPlus} />}
      title="Add members"
      footer={
        <>
          <Button variant="line" size="form" onClick={onClose}>
            Cancel
          </Button>
          <Button
            variant="ink"
            size="form"
            disabled={ids.length === 0 || pending}
            onClick={() => onAdd(ids)}
          >
            {ids.length ? `Add ${plural(ids.length)}` : "Add"}
          </Button>
        </>
      }
    >
      <div
        role="tablist"
        aria-label="Add by"
        className="-mx-6 -mt-1 flex gap-1 border-b border-line px-6 max-[720px]:-mx-4 max-[720px]:px-4"
      >
        <button
          type="button"
          role="tab"
          aria-selected={tab === "search"}
          onClick={() => setTab("search")}
          className={tabClass(tab === "search")}
        >
          Search
        </button>
        <button
          type="button"
          role="tab"
          aria-selected={tab === "paste"}
          onClick={() => setTab("paste")}
          className={tabClass(tab === "paste")}
        >
          Paste IDs
        </button>
      </div>

      <div className="flex min-h-[360px] flex-col gap-3">
        {tab === "search" ? (
          <>
            <label className="flex h-10 items-center gap-2 rounded-[9px] border border-field px-3 text-ink-3">
              <Search className="h-[15px] w-[15px] shrink-0" />
              <input
                type="search"
                aria-label="Search devices"
                placeholder="Device, student ID or name"
                value={query}
                onChange={(e) => setQuery(e.target.value)}
                autoFocus
                className="min-w-0 flex-1 bg-transparent font-mono text-[13px] text-ink outline-none placeholder:text-ink-3"
              />
            </label>
            {term.length < 2 ? (
              <div className="flex flex-col items-center gap-2 py-12 text-ink-3">
                <div aria-hidden="true" className="flex size-11 items-center justify-center rounded-full bg-ground">
                  <Search className="h-5 w-5" />
                </div>
                <span className="text-[13px]">Type 2 or more characters</span>
              </div>
            ) : (
              <div className="flex flex-col gap-2">
                <div className="flex flex-wrap items-center justify-between gap-2 text-xs text-ink-3">
                  <span className="tabular-nums">
                    {results.length} {results.length === 1 ? "match" : "matches"} · {chosen.length} selected
                  </span>
                  {hidden > 0 && <span>{hidden} already in this list, hidden</span>}
                </div>
                {searchQ.isError ? (
                  <ErrorAlert>{errorText(searchQ.error)}</ErrorAlert>
                ) : (
                  <div className="overflow-x-auto rounded-xl border border-line">
                    <table className="w-full border-collapse">
                      <thead>
                        <tr className="bg-row-hover text-left text-xs text-ink-3">
                          <th scope="col" className="w-11 py-2 pl-3.5">
                            <input
                              type="checkbox"
                              aria-label="Select all"
                              checked={allOn}
                              onChange={() => setPicked(allOn ? [] : results.map((d) => d.device_id))}
                              className="size-4 align-middle accent-ink"
                            />
                          </th>
                          <th scope="col" className="px-3 py-2 font-medium max-[720px]:hidden">Device</th>
                          <th scope="col" className="px-3 py-2 font-medium">Student ID</th>
                          <th scope="col" className="px-3 py-2 font-medium">Name</th>
                          <th scope="col" className="py-2 pl-3 pr-3.5 font-medium max-[720px]:hidden">Class</th>
                        </tr>
                      </thead>
                      <tbody>
                        {results.map((d) => {
                          const on = chosen.includes(d.device_id);
                          return (
                            <tr key={d.device_id} className={cn("border-t border-divider", on && "bg-selected")}>
                              <td className="py-2 pl-3.5">
                                <input
                                  type="checkbox"
                                  aria-label={`Select ${d.device_id.slice(0, 8)}`}
                                  checked={on}
                                  onChange={() => toggle(d.device_id)}
                                  className="size-4 align-middle accent-ink"
                                />
                              </td>
                              <td title={d.device_id} className="px-3 py-2.5 font-mono text-[13px] max-[720px]:hidden">
                                {d.device_id.slice(0, 13)}…
                              </td>
                              <td className="px-3 py-2.5 font-mono text-[13px]">{d.student_id ?? "—"}</td>
                              <td className="whitespace-nowrap px-3 py-2.5 font-medium">{d.display_name ?? "—"}</td>
                              <td className="py-2.5 pl-3 pr-3.5 text-ink-2 max-[720px]:hidden">
                                {classLabel(d.device_class)}
                              </td>
                            </tr>
                          );
                        })}
                      </tbody>
                    </table>
                    {searchQ.isSuccess && results.length === 0 && (
                      <div className="p-7 text-center text-[13px] text-ink-3">No devices match</div>
                    )}
                  </div>
                )}
              </div>
            )}
          </>
        ) : (
          <>
            <label className="flex flex-col gap-1.5 text-[13px] font-medium">
              Device IDs
              <textarea
                rows={9}
                value={pasted}
                onChange={(e) => setPasted(e.target.value)}
                placeholder="One per line, or separated by commas or spaces"
                spellCheck={false}
                className="resize-y rounded-[9px] border border-field bg-surface px-3 py-2.5 font-mono text-xs font-normal text-ink placeholder:text-ink-3"
              />
            </label>
            <div className="flex flex-wrap items-center gap-2 text-xs">
              <span className="inline-flex items-center gap-1.5 rounded-full bg-badge-success px-[9px] py-0.5 font-semibold text-badge-success-fg">
                <Check className="h-3 w-3" strokeWidth={2.4} />
                {valid.length} {valid.length === 1 ? "device ID" : "device IDs"}
              </span>
              {invalid.length > 0 && (
                <>
                  <span className="inline-flex items-center gap-1.5 rounded-full bg-badge-danger px-[9px] py-0.5 font-semibold text-badge-danger-fg">
                    <X className="h-3 w-3" strokeWidth={2.4} />
                    {invalid.length} ignored
                  </span>
                  {invalid.map((s, i) => (
                    <span key={i} className="break-all font-mono text-badge-danger-fg line-through">
                      {s}
                    </span>
                  ))}
                </>
              )}
            </div>
          </>
        )}
        {!!error && <ErrorAlert>{errorText(error)}</ErrorAlert>}
      </div>
    </FormDialog>
  );
}
