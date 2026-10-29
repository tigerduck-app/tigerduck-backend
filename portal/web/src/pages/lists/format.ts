// Messages shared by the list and device pages.

import { ApiError } from "@/lib/api";
import type { AddMembersResponse } from "@/types/api";

export function asMessage(e: unknown): string {
  if (e instanceof ApiError) return `HTTP ${e.status} — ${e.message}`;
  if (e instanceof Error) return e.message;
  return String(e);
}

/** "HTTP 500 · list not found", or the status alone when the body says nothing more. */
export function errorText(e: unknown): string {
  if (!(e instanceof ApiError)) return e instanceof Error ? e.message : String(e);
  const status = `HTTP ${e.status}`;
  return e.message === status ? status : `${status} · ${e.message}`;
}

/** "Added 3 to beta · 1 already in list · 2 unknown" */
export function addedLine(r: AddMembersResponse, listName?: string): string {
  return (
    `Added ${r.added}` +
    (listName ? ` to ${listName}` : "") +
    (r.already_present ? ` · ${r.already_present} already in list` : "") +
    (r.unknown ? ` · ${r.unknown} unknown` : "")
  );
}

export function formatTs(ts: string): string {
  return new Date(ts).toLocaleString();
}
