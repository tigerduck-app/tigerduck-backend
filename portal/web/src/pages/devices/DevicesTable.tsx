// The device table and its rows.

import { Smartphone } from "lucide-react";
import { Badge } from "@/components/ui/badge";
import { Card, CardContent } from "@/components/ui/card";
import { Checkbox } from "@/components/ui/checkbox";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";
import type { DeviceRow } from "@/types/api";
import { formatTs } from "./format";

export function DevicesTable({
  rows,
  selected,
  onToggle,
}: {
  rows: DeviceRow[];
  selected: Set<string>;
  onToggle: (id: string) => void;
}) {
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
          <TableHead className="w-10"></TableHead>
          <TableHead>Device ID</TableHead>
          <TableHead>User</TableHead>
          <TableHead>App</TableHead>
          <TableHead>OS</TableHead>
          <TableHead>Push</TableHead>
          <TableHead>Tokens</TableHead>
          <TableHead>Updated</TableHead>
        </TableRow>
      </TableHeader>
      <TableBody>
        {rows.map((d) => (
          <Row
            key={d.device_id}
            d={d}
            checked={selected.has(d.device_id)}
            onToggle={onToggle}
          />
        ))}
      </TableBody>
    </Table>
  );
}

export function Row({
  d,
  checked,
  onToggle,
}: {
  d: DeviceRow;
  checked: boolean;
  onToggle: (id: string) => void;
}) {
  return (
    <TableRow>
      <TableCell>
        <Checkbox
          checked={checked}
          onCheckedChange={() => onToggle(d.device_id)}
        />
      </TableCell>
      <TableCell className="font-mono text-xs break-all">
        {d.device_id}
      </TableCell>
      <TableCell className="font-mono text-xs">{d.user_id || "—"}</TableCell>
      <TableCell className="text-xs">{d.app_version ?? "—"}</TableCell>
      <TableCell className="text-xs">{d.os_version ?? "—"}</TableCell>
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
        {d.has_fcm_token ? (
          <Badge variant="default">fcm</Badge>
        ) : null}
        {!d.has_pts_token && !d.has_device_token && !d.has_fcm_token ? (
          <Badge variant="muted">none</Badge>
        ) : null}
      </TableCell>
      <TableCell className="text-xs text-muted-foreground">
        {formatTs(d.updated_at)}
      </TableCell>
    </TableRow>
  );
}
