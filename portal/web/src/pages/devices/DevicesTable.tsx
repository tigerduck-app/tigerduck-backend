// The device table's columns. The page owns the table state, since its
// platform buttons set the Platform filter.

import { Copy } from "lucide-react";
import { Link } from "react-router-dom";
import type { Column } from "@/components/data-table";
import { Pill } from "@/components/page";
import { copyText } from "@/lib/copy";
import { cn } from "@/lib/cn";
import { ago, stamp } from "@/lib/format";
import { familyLabel } from "@/lib/platform";
import type { DeviceRow } from "@/types/api";
import {
  PLATFORMS,
  appRelease,
  familyColor,
  osName,
  platformIcon,
  platformName,
  tokens,
  tokensLabel,
} from "./format";

const OS_ORDER = ["iOS", "iPadOS", "macOS", "Android"];
const TOKEN_ORDER = ["apns + pts", "apns", "pts", "fcm", "none"];

function rank(order: readonly string[], value: string): number {
  const i = order.indexOf(value);
  return i < 0 ? order.length : i;
}

/** The distinct values, in `order`'s order and newest version first within it. */
function distinct(values: string[], order: (v: string) => number): string[] {
  return [...new Set(values)].sort(
    (a, b) => order(a) - order(b) || b.localeCompare(a, undefined, { numeric: true }),
  );
}

export const shortId = (id: string) => id.slice(0, 8);

function Checkbox(props: { checked: boolean; onChange: () => void; label: string }) {
  return (
    <input
      type="checkbox"
      aria-label={props.label}
      checked={props.checked}
      onChange={props.onChange}
      className="size-4 align-middle accent-ink"
    />
  );
}

/** The checkbox column; added after `useTable`, which it needs the page's rows from. */
export function selectColumn(
  pageRows: DeviceRow[],
  selected: ReadonlySet<string>,
  setSelected: (next: Set<string>) => void,
): Column<DeviceRow> {
  const pageAll = pageRows.length > 0 && pageRows.every((d) => selected.has(d.device_id));
  const toggle = (ids: string[], on: boolean) => {
    const next = new Set(selected);
    for (const id of ids) {
      if (on) next.add(id);
      else next.delete(id);
    }
    setSelected(next);
  };
  return {
    key: "select",
    label: "",
    header: (
      <Checkbox
        label="Select all on this page"
        checked={pageAll}
        onChange={() => toggle(pageRows.map((d) => d.device_id), !pageAll)}
      />
    ),
    className: "w-11 pr-0",
    cell: (d) => (
      <Checkbox
        label={`Select device ${shortId(d.device_id)}`}
        checked={selected.has(d.device_id)}
        onChange={() => toggle([d.device_id], !selected.has(d.device_id))}
      />
    ),
  };
}

const onOff = (on: boolean | null) => (on ? "On" : "Off");

export function deviceColumns(devices: DeviceRow[]): Column<DeviceRow>[] {
  return [
    {
      key: "device",
      label: "Device",
      cell: (d) => (
        <div className="flex items-center gap-1">
          <span title={d.device_id} className="font-mono text-[13px]">
            {shortId(d.device_id)}
          </span>
          <button
            type="button"
            aria-label="Copy device ID"
            onClick={() => copyText(d.device_id, `Copied ${d.device_id}`)}
            className="flex size-7 items-center justify-center rounded-md text-side-muted hover:bg-soft hover:text-ink"
          >
            <Copy className="h-[13px] w-[13px]" />
          </button>
        </div>
      ),
    },
    {
      key: "student",
      label: "Student ID",
      pii: 115,
      sortValue: (d) => d.student_id ?? "",
      className: "font-mono text-[13px]",
      cell: (d) =>
        d.student_id ? (
          <Link to={`/users/${d.user_uuid}`} className="text-link no-underline hover:text-link-hover">
            {d.student_id}
          </Link>
        ) : (
          "—"
        ),
    },
    {
      key: "name",
      label: "Name",
      pii: 130,
      sortValue: (d) => d.display_name ?? "",
      className: "font-medium",
      cell: (d) => d.display_name ?? "—",
    },
    {
      key: "platform",
      label: "Platform",
      sortValue: (d) => platformName(d.platform),
      filterValue: (d) => platformName(d.platform),
      filterOptions: distinct(
        devices.map((d) => platformName(d.platform)),
        (v) => rank(PLATFORMS, v),
      ),
      cell: (d) => {
        const Icon = platformIcon(d.platform);
        return (
          <span className="inline-flex items-center gap-1.5 whitespace-nowrap">
            <Icon
              aria-hidden="true"
              className="h-[15px] w-[15px]"
              style={{ color: familyColor(familyLabel(d.platform)) }}
            />
            {platformName(d.platform)}
          </span>
        );
      },
    },
    {
      key: "app",
      label: "App",
      hide: "phone",
      sortValue: (d) => d.app_version ?? "",
      filterValue: appRelease,
      filterOptions: distinct(devices.map(appRelease), (v) => (v.startsWith("Apple") ? 0 : 1)),
      className: "font-mono text-[13px]",
      cell: (d) => d.app_version ?? "—",
    },
    {
      key: "os",
      label: "OS",
      hide: "phone",
      sortValue: osName,
      filterValue: osName,
      filterOptions: distinct(devices.map(osName), (v) => rank(OS_ORDER, v.split(" ")[0])),
      className: "whitespace-nowrap text-ink-2",
      cell: osName,
    },
    {
      key: "push",
      label: "Push",
      hide: "phone",
      filterValue: (d) => onOff(d.server_push_enabled),
      filterOptions: ["On", "Off"],
      cell: (d) =>
        d.server_push_enabled ? <Pill tone="success">On</Pill> : <Pill className="font-medium">Off</Pill>,
    },
    {
      key: "sync",
      label: "Cloud sync",
      hide: "phone",
      filterValue: (d) => onOff(d.cloud_sync_enabled),
      filterOptions: ["On", "Off"],
      cell: (d) =>
        d.cloud_sync_enabled ? <Pill tone="info">On</Pill> : <Pill className="font-medium">Off</Pill>,
    },
    {
      key: "tokens",
      label: "Tokens",
      hide: "phone",
      filterValue: tokensLabel,
      filterOptions: distinct(devices.map(tokensLabel), (v) => rank(TOKEN_ORDER, v)),
      cell: (d) => (
        <div className="flex gap-1">
          {(tokens(d).length ? tokens(d) : ["none"]).map((t) => (
            <span
              key={t}
              className={cn(
                "rounded-md px-1.5 py-px font-mono text-[11px]",
                t === "none" ? "bg-ground text-ink-3" : "bg-soft text-ink",
              )}
            >
              {t}
            </span>
          ))}
        </div>
      ),
    },
    {
      key: "updated",
      label: "Updated",
      align: "right",
      sortValue: (d) => d.updated_at,
      className: "whitespace-nowrap text-[13px] tabular-nums text-ink-3",
      cell: (d) => <span title={stamp(d.updated_at)}>{ago(d.updated_at)}</span>,
    },
  ];
}
