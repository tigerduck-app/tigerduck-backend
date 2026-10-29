// Custom push: one-off notifications from an operator, to device classes
// narrowed by a list, a course this term, a student or a device. The portal
// files the push jobs itself; the backend's pipeline delivers them.

import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { toast } from "sonner";
import { Send } from "lucide-react";
import { api, errorText } from "@/lib/api";
import { monthDayTime } from "@/lib/format";
import { Button } from "@/components/ui/button";
import { Skeleton } from "@/components/ui/skeleton";
import { DataTable, useTable, type Column } from "@/components/data-table";
import { Dot, EmptyState, ErrorAlert, PageHeading, Panel, Pill } from "@/components/page";
import type { CustomPushRecentItem, CustomPushRequest, CustomPushSendResponse } from "@/types/api";
import { CustomPushDialog } from "./custom-push/CustomPushDialog";
import { CLASSES, ClassIcon, KINDS } from "./custom-push/kinds";

const WEEK_MS = 7 * 24 * 3600 * 1000;
const SHOWN = 30;

function targets(r: CustomPushRecentItem): string {
  if (CLASSES.every((c) => r.target_classes.includes(c.id))) return "All classes";
  return CLASSES.filter((c) => r.target_classes.includes(c.id))
    .map((c) => c.label)
    .join(" + ");
}

function KindPill({ kind }: { kind: CustomPushRecentItem["kind"] }) {
  const { label, icon: Icon, tone } = KINDS[kind];
  return (
    <Pill tone={tone}>
      <Icon aria-hidden="true" className="size-3" strokeWidth={2.4} />
      {label}
    </Pill>
  );
}

const columns: Column<CustomPushRecentItem>[] = [
  {
    key: "request",
    label: "Request",
    sortValue: (r) => r.id,
    hide: "tablet",
    className: "font-mono text-[13px] text-ink-3",
    cell: (r) => <span title={r.id}>{r.id.slice(0, 8)}</span>,
  },
  {
    key: "kind",
    label: "Kind",
    filterValue: (r) => KINDS[r.kind].label,
    filterOptions: ["Record", "Popup"],
    hide: "phone",
    cell: (r) => <KindPill kind={r.kind} />,
  },
  {
    key: "title",
    label: "Title",
    sortValue: (r) => r.title,
    className: "w-full min-w-[200px] max-w-0 phone:min-w-[120px]",
    cell: (r) => (
      <div title={r.title} className="truncate font-medium">
        {r.title}
      </div>
    ),
  },
  {
    key: "targets",
    label: "Targets",
    filterValue: targets,
    hide: "phone",
    cell: (r) => (
      <div className="flex items-center gap-1.5">
        <span className="sr-only">{targets(r)}</span>
        {CLASSES.map((c) => {
          const on = r.target_classes.includes(c.id);
          return (
            <ClassIcon
              key={c.id}
              d={c.d}
              title={on ? c.label : `${c.label} (not targeted)`}
              className={on ? "size-4 shrink-0 text-ink" : "size-4 shrink-0 text-field"}
            />
          );
        })}
      </div>
    ),
  },
  {
    key: "devices",
    label: "Devices",
    sortValue: (r) => (r.is_queueing ? -1 : r.total),
    align: "right",
    className: "tabular-nums",
    cell: (r) => (r.is_queueing ? <Pill tone="warning">Queueing</Pill> : r.total.toLocaleString("en-US")),
  },
  {
    key: "sent",
    label: "Sent",
    sortValue: (r) => r.sent_at ?? "",
    className: "whitespace-nowrap text-[13px] tabular-nums text-ink-2",
    cell: (r) => (r.sent_at ? monthDayTime(r.sent_at) : "—"),
  },
];

export function CustomPushPage() {
  const qc = useQueryClient();
  const [composing, setComposing] = useState(false);

  // shortcut: the 7-day tiles count the latest 100 sends; add a stats route if a week ever holds more.
  const recentQ = useQuery<CustomPushRecentItem[]>({
    queryKey: ["custom-push-recent"],
    queryFn: () => api<CustomPushRecentItem[]>("/api/custom-push/recent?limit=100"),
    // Fast while a send is still queueing, so its count fills in; slower otherwise.
    refetchInterval: (q) => (q.state.data?.some((r) => r.is_queueing) ? 2000 : 10_000),
    refetchOnWindowFocus: true,
  });

  const sendMut = useMutation({
    mutationFn: (req: CustomPushRequest) =>
      api<CustomPushSendResponse>("/api/custom-push", { method: "POST", json: req }),
    onSuccess: (r) => {
      toast.success(`Queued ${r.queued.toLocaleString("en-US")} devices · request ${r.request_id.slice(0, 8)}`);
      setComposing(false);
      qc.invalidateQueries({ queryKey: ["custom-push-recent"] });
    },
    onError: (e) => toast.error(`Send failed · ${errorText(e)}`),
  });

  const sends = recentQ.data;
  const table = useTable({
    rows: sends?.slice(0, SHOWN) ?? [],
    columns,
    initialSort: { key: "sent", dir: "desc" },
  });
  const going = sends?.filter((s) => s.is_queueing) ?? [];
  const now = Date.now();
  const week = sends?.filter((s) => s.sent_at && now - Date.parse(s.sent_at) < WEEK_MS) ?? [];
  const last = sends?.find((s) => s.sent_at);
  const tiles = [
    {
      label: "Sends, 7 days",
      value: String(week.length),
      note: `${week.filter((s) => s.kind === "record").length} record · ${week.filter((s) => s.kind === "popup").length} popup`,
    },
    {
      label: "Devices reached, 7 days",
      value: week.reduce((n, s) => n + s.total, 0).toLocaleString("en-US"),
      note: "Sum of matched devices",
    },
    { label: "Last send", value: last?.sent_at ? monthDayTime(last.sent_at) : "—", note: last?.title ?? "" },
  ];
  const open = () => {
    sendMut.reset();
    setComposing(true);
  };
  const newButton = (
    <Button variant="ink" size="form" onClick={open}>
      <Send />
      New push
    </Button>
  );

  return (
    <>
      <PageHeading group="Push" title="Custom push" actions={newButton} />

      {going.length > 0 && (
        <Panel
          title={
            <span className="flex items-center gap-2">
              <Dot tone="warn" />
              Sending now
            </span>
          }
          aside={<span className="text-xs text-ink-3 phone:hidden">Updates every 2 s</span>}
        >
          <ul>
            {going.map((g) => (
              <li
                key={g.id}
                className="flex flex-wrap items-center gap-x-5 gap-y-3 border-t border-divider px-6 py-3.5 first:border-t-0 phone:px-4"
              >
                <span className="font-mono text-[13px] text-ink-3 phone:hidden">{g.id.slice(0, 8)}</span>
                <KindPill kind={g.kind} />
                <span className="min-w-0 flex-[1_1_240px] truncate font-medium">{g.title}</span>
                <div aria-hidden="true" className="h-1.5 flex-[0_1_220px] overflow-hidden rounded-full bg-divider">
                  <div className="h-full w-2/5 animate-pulse rounded-full bg-dot-warn motion-reduce:animate-none" />
                </div>
                <span className="text-[13px] text-ink-2">Queueing</span>
              </li>
            ))}
          </ul>
        </Panel>
      )}

      {sends && sends.length > 0 && (
        <div className="grid grid-cols-[repeat(auto-fit,minmax(220px,1fr))] gap-3">
          {tiles.map((k) => (
            <div key={k.label} className="flex min-w-0 flex-col gap-1 rounded-[14px] border border-line bg-surface px-[18px] py-3.5">
              <span className="text-xs font-medium text-ink-3">{k.label}</span>
              <span className="text-[22px] font-semibold tabular-nums">{k.value}</span>
              <span className="truncate text-xs text-ink-2">{k.note}</span>
            </div>
          ))}
        </div>
      )}

      <Panel title="Recent sends" aside={<span className="text-xs text-ink-3">Last 30</span>}>
        {recentQ.isLoading && <Skeleton className="m-6 h-40" />}
        {recentQ.isError && (
          <ErrorAlert className="m-6 items-center" onRetry={() => recentQ.refetch()}>
            Couldn’t load recent sends
            <div className="font-mono text-xs">{errorText(recentQ.error)}</div>
          </ErrorAlert>
        )}
        {sends && sends.length === 0 && <EmptyState icon={Send} title="No sends yet" action={newButton} />}
        {sends && sends.length > 0 && (
          <DataTable table={table} rowKey={(r) => r.id} minWidth={860} empty="No sends match" />
        )}
      </Panel>

      {composing && (
        <CustomPushDialog
          pending={sendMut.isPending}
          onClose={() => setComposing(false)}
          onSend={(req) => sendMut.mutate(req)}
        />
      )}
    </>
  );
}
