// One user's page, opened from the Status user list: their devices, this
// week's classes and the pushes of the last 7 days, from /api/users/<id>.

import { useQuery } from "@tanstack/react-query";
import { Link, useParams } from "react-router-dom";
import { Check, ChevronLeft, Copy, GraduationCap, Minus, Send, Smartphone, UserMinus, UserX, X } from "lucide-react";
import { api, ApiError } from "@/lib/api";
import { cn } from "@/lib/cn";
import { copyText } from "@/lib/copy";
import { fullDate, monthDayTime } from "@/lib/format";
import { platformLabel } from "@/lib/platform";
import { Button } from "@/components/ui/button";
import { Skeleton } from "@/components/ui/skeleton";
import { DataTable, useTable, type Column } from "@/components/data-table";
import { Dot, EmptyState, ErrorAlert, Panel, Pill, type Tone } from "@/components/page";
import type { AccountState, UserDetail, UserDevice, UserPush } from "@/types/api";
import { Card } from "./status/Card";
import { ACCOUNT, PLATFORM_ICON, ago, termLabel } from "./status/format";
import { Timetable } from "./user-detail/Timetable";

const ACCOUNT_LINE: Record<AccountState, string> = {
  signed_in: "School account signed in",
  needs_sign_in: "School account needs sign-in",
  failing: "School account failing",
};
const CHANNEL: Record<string, [string, Tone]> = {
  schedule: ["Live Activity", "info"],
  course: ["Class reminder", "info"],
  assignment: ["Assignment", "orange"],
  bulletin: ["Bulletin", "violet"],
  custom: ["Custom push", "neutral"],
  system: ["System", "neutral"],
};
const STATUS: Record<UserPush["status"], [string, typeof Check, string]> = {
  sent: ["Delivered", Check, "text-dot-ok"],
  failed: ["Failed", X, "text-dot-bad"],
  cancelled: ["Cancelled", Minus, "text-ink-3"],
};

const deviceName = (d: UserDevice) => d.device_name || platformLabel(d.platform);
const channelOf = (p: UserPush) => CHANNEL[p.channel] ?? [p.channel, "neutral" as Tone];

function localDay(ms: number): string {
  return new Date(ms).toLocaleDateString("en-CA");
}

/** "Today 08:31", "Yesterday 22:10", "Oct 26 14:00" */
function pushTime(iso: string): string {
  const ms = Date.parse(iso);
  const time = new Date(ms).toLocaleTimeString("en-GB", { hour: "2-digit", minute: "2-digit" });
  if (localDay(ms) === localDay(Date.now())) return `Today ${time}`;
  if (localDay(ms) === localDay(Date.now() - 86_400_000)) return `Yesterday ${time}`;
  return monthDayTime(iso);
}

export function UserDetailPage() {
  const { id = "" } = useParams();
  const q = useQuery<UserDetail>({
    queryKey: ["user", id],
    queryFn: () => api<UserDetail>(`/api/users/${encodeURIComponent(id)}`),
    refetchInterval: 60_000,
    retry: (n, e) => !(e instanceof ApiError && e.status === 404) && n < 2,
  });
  const missing = q.error instanceof ApiError && q.error.status === 404;

  return (
    <>
      <nav aria-label="Breadcrumb" className="flex items-center gap-1.5 text-[13px] text-ink-3">
        <Link to="/" className="inline-flex min-h-8 items-center gap-1 text-link no-underline hover:text-link-hover">
          <ChevronLeft className="h-3.5 w-3.5" />
          Status
        </Link>
        <span aria-hidden="true">/</span>
        <span>Users</span>
      </nav>
      {q.isLoading && <Skeleton className="h-[480px] w-full rounded-[14px]" />}
      {missing && (
        <Panel>
          <EmptyState icon={UserX} title="User not found" />
        </Panel>
      )}
      {q.isError && !missing && (
        <ErrorAlert className="items-center" onRetry={() => q.refetch()}>
          Failed to load user: {(q.error as Error).message}
        </ErrorAlert>
      )}
      {q.data && <UserContent user={q.data} />}
    </>
  );
}

function UserContent({ user }: { user: UserDetail }) {
  const { devices, pushes } = user;
  const byId = new Map(devices.map((d) => [d.id, d]));
  const lastActive = devices.map((d) => d.last_seen_at).filter(Boolean).sort().pop() ?? null;
  const cloudSync = devices.length ? devices.some((d) => d.cloud_sync_enabled) : null;
  const settled = pushes.filter((p) => p.status !== "cancelled");
  const failed = pushes.filter((p) => p.status === "failed").length;
  const syncDevice = user.last_sync && byId.get(user.last_sync.device_id);
  const syncMs = user.last_sync ? Date.parse(user.last_sync.at) : 0;
  const syncDay =
    localDay(syncMs) === localDay(Date.now())
      ? "Today"
      : localDay(syncMs) === localDay(Date.now() - 86_400_000)
        ? "Yesterday"
        : user.last_sync
          ? fullDate(user.last_sync.at)
          : "";

  const tiles = [
    {
      label: "Devices",
      value: String(devices.length),
      note: [...new Set(devices.map((d) => platformLabel(d.platform)))].join(", "),
    },
    {
      label: user.term ? `Courses in ${termLabel(user.term)}` : "Courses",
      value: String(user.courses.length),
      note: "",
    },
    { label: "Pushes, last 7 days", value: String(settled.length), note: `${failed} failed` },
    {
      label: "Last sync",
      value: user.last_sync
        ? new Date(syncMs).toLocaleTimeString("en-GB", { hour: "2-digit", minute: "2-digit" })
        : "—",
      note: user.last_sync ? `${syncDay}${syncDevice ? `, from ${deviceName(syncDevice)}` : ""}` : "",
    },
  ];

  return (
    <>
      <header className="-mt-3 flex flex-wrap items-center justify-between gap-4">
        <div className="flex min-w-0 items-center gap-4">
          <div
            aria-hidden="true"
            className="flex size-14 shrink-0 items-center justify-center rounded-full bg-badge-info text-[22px] font-semibold text-badge-info-fg"
          >
            {(user.display_name || user.student_id || "?").charAt(0)}
          </div>
          <div className="flex min-w-0 flex-col gap-1">
            <div className="flex flex-wrap items-center gap-x-2.5">
              <h1 className="break-all text-[28px] font-semibold leading-tight tracking-[-0.01em] phone:text-[22px]">
                {user.display_name || user.student_id || "—"}
              </h1>
              {user.student_id && (
                <span className="flex items-center gap-0.5">
                  <span className="font-mono text-[15px] text-ink-2">{user.student_id}</span>
                  <button
                    type="button"
                    aria-label="Copy student ID"
                    title="Copy student ID"
                    onClick={() => copyText(user.student_id!, "Copied student ID")}
                    className="inline-flex size-8 items-center justify-center rounded-lg text-ink-3 hover:bg-soft hover:text-ink"
                  >
                    <Copy className="h-[15px] w-[15px]" />
                  </button>
                </span>
              )}
            </div>
            <div className="flex flex-wrap items-center gap-x-3.5 gap-y-1.5 text-[13px] text-ink-2">
              {user.account && (
                <span className="inline-flex items-center gap-1.5">
                  <Dot tone={ACCOUNT[user.account].tone} />
                  {ACCOUNT_LINE[user.account]}
                </span>
              )}
              {cloudSync !== null && (
                <Pill tone={cloudSync ? "success" : "neutral"}>
                  {cloudSync ? "Cloud sync on" : "Cloud sync off"}
                </Pill>
              )}
              <span>Joined {fullDate(user.created_at)}</span>
              {lastActive && <span>Last active {ago(lastActive)}</span>}
              {user.locale && <span>{user.locale}</span>}
            </div>
          </div>
        </div>
        <div className="flex flex-wrap gap-2">
          <Button asChild variant="line" size="form" className="[&_svg]:size-[15px]">
            <Link to="/custom-push">
              <Send />
              Send push
            </Link>
          </Button>
          <Button asChild variant="line" size="form" className="[&_svg]:size-[15px]">
            <Link to="/inspect">
              <GraduationCap />
              Moodle data
            </Link>
          </Button>
          <Button
            asChild
            variant="line"
            size="form"
            className="border-danger/40 text-danger hover:bg-badge-danger [&_svg]:size-[15px]"
          >
            <Link to="/deregister">
              <UserMinus />
              Deregister
            </Link>
          </Button>
        </div>
      </header>

      <div className="grid grid-cols-[repeat(auto-fit,minmax(200px,1fr))] gap-3 phone:grid-cols-2">
        {tiles.map((t) => (
          <div key={t.label} className="flex min-w-0 flex-col gap-0.5 rounded-[14px] border border-line bg-surface px-[18px] py-3.5">
            <span className="text-xs font-medium text-ink-3">{t.label}</span>
            <span className="text-2xl font-semibold tabular-nums">{t.value}</span>
            {t.note && (
              <span title={t.note} className="truncate text-xs text-ink-2">
                {t.note}
              </span>
            )}
          </div>
        ))}
      </div>

      <DevicesPanel devices={devices} classReminders={user.class_reminders} />

      {user.term && (
        <Card title={`This week in ${termLabel(user.term)}`}>
          <Timetable courses={user.courses} weekStart={user.week_start} />
        </Card>
      )}

      <PushesPanel pushes={pushes} byId={byId} />
    </>
  );
}

function Toggle({ on, label, title }: { on: boolean; label: string; title: string }) {
  const Icon = on ? Check : X;
  return (
    <span
      title={`${title} ${on ? "on" : "off"}`}
      className={cn(
        "inline-flex items-center gap-1 rounded-full px-2 py-0.5 text-xs font-medium",
        on ? "bg-badge-success text-badge-success-fg" : "bg-ground text-ink-3",
      )}
    >
      <Icon className="h-3 w-3" strokeWidth={2.4} />
      {label}
    </span>
  );
}

function tokenLabel(d: UserDevice): string {
  const t = d.push_token;
  if (!t) return "No token";
  if (t.environment) return t.environment.charAt(0).toUpperCase() + t.environment.slice(1);
  return t.provider.toUpperCase();
}

function DevicesPanel({ devices, classReminders }: { devices: UserDevice[]; classReminders: boolean }) {
  const columns: Column<UserDevice>[] = [
    {
      key: "device",
      label: "Device",
      sortValue: deviceName,
      cell: (d) => {
        const Icon = PLATFORM_ICON[d.platform] ?? Smartphone;
        return (
          <div className="flex items-center gap-3">
            <span aria-hidden="true" className="flex size-9 shrink-0 items-center justify-center rounded-[10px] bg-ground text-ink-2">
              <Icon className="h-[18px] w-[18px]" />
            </span>
            <div className="flex min-w-0 flex-col">
              <span className="font-medium">{deviceName(d)}</span>
              {d.device_model && <span className="text-xs text-ink-3">{d.device_model}</span>}
            </div>
          </div>
        );
      },
    },
    {
      key: "platform",
      label: "Platform",
      sortValue: (d) => platformLabel(d.platform),
      hide: "phone",
      className: "whitespace-nowrap",
      cell: (d) => (
        <>
          {platformLabel(d.platform)} <span className="text-ink-3">{d.os_version}</span>
        </>
      ),
    },
    {
      key: "app",
      label: "App",
      sortValue: (d) => d.app_version ?? "",
      hide: "phone",
      className: "font-mono text-[13px]",
      cell: (d) => d.app_version ?? "—",
    },
    {
      key: "toggles",
      label: "Turned on",
      hide: "phone",
      cell: (d) => (
        <div className="flex flex-wrap gap-1.5">
          <Toggle on={classReminders} label="Class" title="Class reminders" />
          <Toggle on={d.bulletin_push_enabled} label="Bulletins" title="Bulletin pushes" />
          <Toggle on={d.sync_live_activity} label="Live Activity" title="Live Activities" />
          <Toggle on={d.cloud_sync_enabled} label="Sync" title="Cloud sync" />
        </div>
      ),
    },
    {
      key: "token",
      label: "Push token",
      sortValue: tokenLabel,
      className: "whitespace-nowrap text-[13px]",
      cell: (d) => (
        <span className="inline-flex items-center gap-1.5">
          <Dot tone={d.push_token ? "ok" : "off"} />
          {tokenLabel(d)}
        </span>
      ),
    },
    {
      key: "seen",
      label: "Last seen",
      sortValue: (d) => (d.last_seen_at ? -Date.parse(d.last_seen_at) : Infinity),
      className: "whitespace-nowrap text-[13px] tabular-nums text-ink-2",
      cell: (d) => (d.last_seen_at ? ago(d.last_seen_at) : "—"),
    },
  ];
  const table = useTable({ rows: devices, columns, perPage: Infinity, initialSort: { key: "seen", dir: "asc" } });
  return (
    <Panel title="Devices">
      <DataTable table={table} rowKey={(d) => d.id} minWidth={860} empty="No devices" />
    </Panel>
  );
}

function PushesPanel({ pushes, byId }: { pushes: UserPush[]; byId: Map<string, UserDevice> }) {
  const device = (p: UserPush) => {
    const d = p.device_id ? byId.get(p.device_id) : undefined;
    return d ? deviceName(d) : null;
  };
  const columns: Column<UserPush>[] = [
    {
      key: "time",
      label: "Time",
      sortValue: (p) => p.at,
      className: "whitespace-nowrap py-2.5 text-[13px] tabular-nums",
      cell: (p) => pushTime(p.at),
    },
    {
      key: "channel",
      label: "Channel",
      sortValue: (p) => channelOf(p)[0],
      filterValue: (p) => channelOf(p)[0],
      hide: "phone",
      className: "whitespace-nowrap py-2.5",
      cell: (p) => <Pill tone={channelOf(p)[1]}>{channelOf(p)[0]}</Pill>,
    },
    {
      key: "title",
      label: "Title",
      className: "min-w-[240px] py-2.5 phone:min-w-0",
      cell: (p) => p.title || <span className="font-mono text-xs text-ink-3">{p.scenario}</span>,
    },
    {
      key: "device",
      label: "Device",
      sortValue: (p) => device(p) ?? "",
      filterValue: device,
      hide: "phone",
      className: "whitespace-nowrap py-2.5 text-[13px] text-ink-2",
      cell: (p) => device(p) ?? "—",
    },
    {
      key: "status",
      label: "Status",
      sortValue: (p) => STATUS[p.status][0],
      filterValue: (p) => STATUS[p.status][0],
      filterOptions: Object.values(STATUS).map(([label]) => label),
      className: "whitespace-nowrap py-2.5 text-[13px]",
      cell: (p) => {
        const [label, Icon, color] = STATUS[p.status];
        return (
          <span className="inline-flex items-center gap-1.5">
            <Icon className={cn("h-3.5 w-3.5", color)} strokeWidth={2.4} />
            {label}
          </span>
        );
      },
    },
  ];
  const table = useTable({ rows: pushes, columns, perPage: 8, initialSort: { key: "time", dir: "desc" } });
  return (
    <Panel title="Recent pushes" aside={<span className="text-xs text-ink-3">last 7 days</span>}>
      <DataTable
        table={table}
        rowKey={(p) => p.id}
        minWidth={820}
        empty={pushes.length ? "No pushes match these filters" : "No pushes in the last 7 days"}
      />
    </Panel>
  );
}
