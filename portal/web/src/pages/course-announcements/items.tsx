// A review item's change, its checks and its actions, as the Review table
// and the announcement's detail both show them.

import { Check as CheckIcon, Pencil, X } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Pill } from "@/components/page";
import { DetailCell } from "../course-changes/tables";
import { dateText, KIND, type Change, type Course } from "../course-changes/model";
import { ACTION_LABEL, asChange, checksOf, type Item } from "./model";

export type ItemHandlers = {
  onApprove: (item: Item) => void;
  /** Absent where the change cannot be opened in the editor. */
  onEdit?: (item: Item) => void;
  onReject: (item: Item) => void;
  busy: boolean;
};

export function KindPills({ item, term }: { item: Item; term: string }) {
  const kind = KIND[asChange(item, term).kind];
  const action = item.action && ACTION_LABEL[item.action];
  return (
    <span className="inline-flex flex-wrap gap-1">
      {kind && <Pill tone={kind.tone}>{kind.label}</Pill>}
      {action && <Pill>{action}</Pill>}
    </span>
  );
}

/** The item's day and change; a remove shows the change it removes. Under an
 * add or replace, the change it clashes with or replaces. */
export function ChangeDetail({
  item,
  term,
  course,
  existing,
}: {
  item: Item;
  term: string;
  course: Course | undefined;
  existing: Change | undefined;
}) {
  const own = asChange(item, term);
  const main = item.action === "remove" && existing ? existing : own;
  return (
    <div className="flex min-w-0 flex-col items-start gap-1">
      {KIND[main.kind] && (
        <>
          <span className="font-medium">{dateText(main)}</span>
          <DetailCell change={main} course={course} />
        </>
      )}
      {existing && main !== existing && (
        <div className="flex flex-wrap items-center gap-x-1.5 gap-y-1 text-xs text-ink-3">
          <span>Existing change</span>
          <span className="text-ink-2">{dateText(existing)}</span>
          <DetailCell change={existing} course={course} />
        </div>
      )}
    </div>
  );
}

/** "Passes all checks", or a pill per check the item fails. */
export function CheckPills({ item }: { item: Item }) {
  const failed = checksOf(item.checks).filter((c) => !c.ok);
  return (
    <div className="flex max-w-[220px] flex-wrap gap-1">
      {item.passes_checks ? (
        <Pill tone="success" className="px-[7px] py-px text-[11px]">
          Passes all checks
        </Pill>
      ) : (
        failed.map((c) => (
          <Pill key={c.code} tone="warning" className="px-[7px] py-px text-[11px]">
            {c.failed}
          </Pill>
        ))
      )}
    </div>
  );
}

export function ItemActions({ item, onApprove, onEdit, onReject, busy }: ItemHandlers & { item: Item }) {
  return (
    <div className="inline-flex items-center gap-1 phone:max-w-[110px] phone:flex-wrap phone:justify-end">
      <Button variant="ink" size="sm" className="rounded-[9px] [&_svg]:size-3.5" disabled={busy} onClick={() => onApprove(item)}>
        <CheckIcon strokeWidth={2.4} />
        Approve
      </Button>
      {onEdit && (
        <Button
          variant="quiet"
          size="iconSm"
          aria-label="Edit and approve"
          title="Edit and approve"
          disabled={busy}
          onClick={() => onEdit(item)}
        >
          <Pencil />
        </Button>
      )}
      <Button variant="quiet" size="iconSm" aria-label="Reject" title="Reject" disabled={busy} onClick={() => onReject(item)}>
        <X />
      </Button>
    </div>
  );
}
