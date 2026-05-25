import { useQuery } from "@tanstack/react-query";
import { Smartphone } from "lucide-react";
import { Badge } from "@/components/ui/badge";
import { Card, CardContent } from "@/components/ui/card";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";
import { PageHeader } from "@/components/ui/section";
import { Skeleton } from "@/components/ui/skeleton";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs";
import { api } from "@/lib/api";
import type { DevicesPayload, DeviceRow } from "@/types/api";

// Sub-tab key → matcher on the device row. Apple `device_class` is
// either "iphone" or "ipad"; Android registers without an apns env so we
// gate on `platform` instead of `device_class` to catch the (rare but
// real) older rows where `device_class` was left blank.
const TABS: Array<{
  key: "iphone" | "ipad" | "android";
  label: string;
  match: (d: DeviceRow) => boolean;
}> = [
  { key: "iphone", label: "iPhone", match: (d) => d.device_class === "iphone" },
  { key: "ipad", label: "iPad", match: (d) => d.device_class === "ipad" },
  {
    key: "android",
    label: "Android",
    match: (d) => d.platform === "android",
  },
];

export function DevicesPage() {
  const q = useQuery<DevicesPayload>({
    queryKey: ["devices"],
    queryFn: () => api<DevicesPayload>("/api/devices?limit=500"),
    refetchInterval: 30_000,
  });

  return (
    <div className="space-y-6">
      <PageHeader
        title="Registered devices"
        description="Every row in device_registrations. Newest activity first."
      />

      {q.isLoading && <Skeleton className="h-48" />}
      {q.isError && (
        <Card>
          <CardContent className="text-sm text-destructive">
            Failed to load devices: {(q.error as Error).message}
          </CardContent>
        </Card>
      )}
      {q.data && (
        <Tabs defaultValue="iphone" className="space-y-4">
          <TabsList>
            {TABS.map((t) => {
              const count = q.data.items.filter(t.match).length;
              return (
                <TabsTrigger key={t.key} value={t.key}>
                  {t.label}
                  <span className="ml-1.5 text-xs text-muted-foreground">
                    {count}
                  </span>
                </TabsTrigger>
              );
            })}
          </TabsList>
          {TABS.map((t) => {
            const rows = q.data!.items.filter(t.match);
            return (
              <TabsContent key={t.key} value={t.key}>
                <DevicesTable rows={rows} />
              </TabsContent>
            );
          })}
        </Tabs>
      )}
    </div>
  );
}

function DevicesTable({ rows }: { rows: DeviceRow[] }) {
  if (rows.length === 0) {
    return (
      <Card>
        <CardContent className="flex items-center gap-2 text-sm text-muted-foreground">
          <Smartphone className="h-4 w-4" /> No devices in this category yet.
        </CardContent>
      </Card>
    );
  }
  return (
    <Table>
      <TableHeader>
        <TableRow>
          <TableHead>Device ID</TableHead>
          <TableHead>User</TableHead>
          <TableHead>Bundle</TableHead>
          <TableHead>APNs env</TableHead>
          <TableHead>Push</TableHead>
          <TableHead>Tokens</TableHead>
          <TableHead>Updated</TableHead>
        </TableRow>
      </TableHeader>
      <TableBody>
        {rows.map((d) => (
          <Row key={d.device_id} d={d} />
        ))}
      </TableBody>
    </Table>
  );
}

function Row({ d }: { d: DeviceRow }) {
  return (
    <TableRow>
      <TableCell className="font-mono text-xs">
        <span className="block max-w-[16ch] truncate" title={d.device_id}>
          {d.device_id}
        </span>
      </TableCell>
      <TableCell className="font-mono text-xs">{d.user_id || "—"}</TableCell>
      <TableCell className="font-mono text-xs text-muted-foreground">
        {d.bundle_id || "—"}
      </TableCell>
      <TableCell className="text-sm text-muted-foreground">
        {d.apns_env || "—"}
      </TableCell>
      <TableCell>
        {d.server_push_enabled ? (
          <Badge variant="success">on</Badge>
        ) : (
          <Badge variant="muted">off</Badge>
        )}
      </TableCell>
      <TableCell className="space-x-1">
        {d.has_pts_token ? (
          <Badge variant="default">pts</Badge>
        ) : null}
        {d.has_device_token ? (
          <Badge variant="default">apns</Badge>
        ) : null}
        {!d.has_pts_token && !d.has_device_token ? (
          <Badge variant="muted">none</Badge>
        ) : null}
      </TableCell>
      <TableCell className="text-xs text-muted-foreground">
        {formatTs(d.updated_at)}
      </TableCell>
    </TableRow>
  );
}

function formatTs(ts: string): string {
  try {
    return new Date(ts).toLocaleString();
  } catch {
    return ts;
  }
}
