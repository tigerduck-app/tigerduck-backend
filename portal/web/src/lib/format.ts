// Short English dates for the redesigned tables, in the browser's time zone
// unless `utc` is set for a date-only value such as an expiry day.

const MONTH_DAY = { month: "short", day: "numeric" } as const;

/** "Oct 12" */
export function monthDay(iso: string, utc = false): string {
  return new Date(iso).toLocaleDateString("en-US", { ...MONTH_DAY, ...(utc && { timeZone: "UTC" }) });
}

/** "Oct 29 08:00" */
export function monthDayTime(iso: string): string {
  const d = new Date(iso);
  return `${d.toLocaleDateString("en-US", MONTH_DAY)} ${d.toLocaleTimeString("en-GB", { hour: "2-digit", minute: "2-digit" })}`;
}

/** "Oct 29, 2026" */
export function fullDate(iso: string, utc = false): string {
  return new Date(iso).toLocaleDateString("en-US", {
    ...MONTH_DAY,
    year: "numeric",
    ...(utc && { timeZone: "UTC" }),
  });
}

/** "2.3 KB" */
export function fileSize(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`;
  const units = ["KB", "MB", "GB"];
  let n = bytes / 1024;
  let i = 0;
  while (n >= 1024 && i < units.length - 1) {
    n /= 1024;
    i += 1;
  }
  return `${n.toFixed(1)} ${units[i]}`;
}
