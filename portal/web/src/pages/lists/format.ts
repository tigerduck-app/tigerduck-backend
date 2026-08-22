// Formatting shared by the list pages.

import { ApiError } from "@/lib/api";

export function formatTs(ts: string): string {
  try {
    return new Date(ts).toLocaleString();
  } catch {
    return ts;
  }
}

export function asMessage(e: unknown): string {
  if (e instanceof ApiError) return `HTTP ${e.status} — ${e.message}`;
  if (e instanceof Error) return e.message;
  return String(e);
}
