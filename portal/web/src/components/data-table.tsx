// The list table every redesigned page shares: sortable headers, per-column
// filter menus with counts, filter chips, paging, and student-data columns
// that stay collapsed until the operator reveals them.

import * as React from "react";
import {
  ArrowDown,
  ArrowUp,
  ChevronLeft,
  ChevronRight,
  ChevronsUpDown,
  Eye,
  EyeOff,
  Funnel,
  X,
} from "lucide-react";
import {
  DropdownMenu,
  DropdownMenuCheckboxItem,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuLabel,
  DropdownMenuSeparator,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";
import { cn } from "@/lib/cn";

export type Column<R> = {
  key: string;
  /** Empty for an actions column. */
  label: string;
  cell: (row: R) => React.ReactNode;
  /** Makes the header sort by this value. */
  sortValue?: (row: R) => string | number;
  /** Adds a filter menu listing these values with their counts. */
  filterValue?: (row: R) => string;
  /** The filter menu's values in this order; otherwise as they first appear. */
  filterOptions?: readonly string[];
  align?: "right";
  hide?: "tablet" | "phone";
  /** Student IDs and names: collapsed to nothing until revealed, up to this width. */
  pii?: number;
  /** Extra classes for this column's cells. */
  className?: string;
};

export type SortState = { key: string; dir: "asc" | "desc" };
export type Filters = Record<string, readonly string[]>;
export type FilterOption = { value: string; count: number; checked: boolean };

export type TableModel<R> = {
  columns: readonly Column<R>[];
  /** The rows on the current page. */
  rows: R[];
  /** Rows left after the filters and `match`. */
  total: number;
  page: number;
  pages: number;
  perPage: number;
  sort: SortState | null;
  filters: Filters;
  toggleSort: (key: string) => void;
  setFilter: (key: string, values: readonly string[]) => void;
  clearFilters: () => void;
  setPage: (page: number) => void;
  options: (column: Column<R>) => FilterOption[];
};

function compare(a: string | number, b: string | number): number {
  if (typeof a === "number" && typeof b === "number") return a - b;
  return String(a).localeCompare(String(b), undefined, { numeric: true });
}

/**
 * Sorting, filtering and paging state for a `DataTable`. `match` narrows the
 * rows further, for a search box; `perPage: Infinity` turns paging off.
 */
export function useTable<R>({
  rows,
  columns,
  perPage = 10,
  initialSort = null,
  match,
}: {
  rows: readonly R[];
  columns: readonly Column<R>[];
  perPage?: number;
  initialSort?: SortState | null;
  match?: (row: R) => boolean;
}): TableModel<R> {
  const [sort, setSort] = React.useState<SortState | null>(initialSort);
  const [filters, setFilters] = React.useState<Filters>({});
  const [page, setPage] = React.useState(1);

  const filtered = rows.filter(
    (r) =>
      columns.every((c) => {
        const chosen = filters[c.key];
        return !chosen?.length || !c.filterValue || chosen.includes(c.filterValue(r));
      }) && (!match || match(r)),
  );
  const sortColumn = sort && columns.find((c) => c.key === sort.key);
  const sorted = sortColumn?.sortValue
    ? filtered
        .map((r) => [sortColumn.sortValue!(r), r] as const)
        .sort(([a], [b]) => (sort!.dir === "asc" ? compare(a, b) : compare(b, a)))
        .map(([, r]) => r)
    : filtered;
  const pages = Math.max(1, Math.ceil(sorted.length / perPage));
  const current = Math.min(page, pages);
  const start = Number.isFinite(perPage) ? (current - 1) * perPage : 0;

  return {
    columns,
    rows: sorted.slice(start, start + perPage),
    total: sorted.length,
    page: current,
    pages,
    perPage,
    sort,
    filters,
    toggleSort: (key) => {
      setSort((s) => ({ key, dir: s?.key === key && s.dir === "asc" ? "desc" : "asc" }));
      setPage(1);
    },
    setFilter: (key, values) => {
      setFilters((f) => ({ ...f, [key]: values }));
      setPage(1);
    },
    clearFilters: () => {
      setFilters({});
      setPage(1);
    },
    setPage,
    options: (column) => {
      const value = column.filterValue;
      if (!value) return [];
      const values = column.filterOptions ?? [...new Set(rows.map(value))];
      const chosen = filters[column.key] ?? [];
      return values.map((v) => ({
        value: v,
        count: rows.filter((r) => value(r) === v).length,
        checked: chosen.includes(v),
      }));
    },
  };
}

const HIDE = { tablet: "tablet:hidden", phone: "phone:hidden" } as const;

export function DataTable<R>({
  table,
  rowKey,
  minWidth = 720,
  empty,
  showPii = false,
}: {
  table: TableModel<R>;
  rowKey: (row: R) => string;
  /** Below this width the table scrolls sideways; dropped at tablet width. */
  minWidth?: number;
  /** Shown under the headers when no row matches. */
  empty: React.ReactNode;
  showPii?: boolean;
}) {
  const active = table.columns.filter((c) => table.filters[c.key]?.length);
  return (
    <>
      {active.length > 0 && (
        <div className="flex flex-wrap items-center gap-1.5 border-b border-divider px-6 py-2.5 phone:px-4">
          {active.map((c) => (
            <span
              key={c.key}
              className="inline-flex min-h-7 items-center gap-1 rounded-full bg-soft pl-2.5 pr-1 text-xs text-ink"
            >
              {c.label}: {table.filters[c.key].join(", ")}
              <button
                type="button"
                aria-label="Remove filter"
                onClick={() => table.setFilter(c.key, [])}
                className="flex size-6 items-center justify-center rounded-full text-ink-2 hover:bg-field/50"
              >
                <X className="h-3 w-3" strokeWidth={2.4} />
              </button>
            </span>
          ))}
          <button
            type="button"
            onClick={table.clearFilters}
            className="min-h-7 px-2 text-xs text-link hover:text-link-hover"
          >
            Clear filters
          </button>
        </div>
      )}
      <div className="overflow-x-auto">
        <table
          style={{ "--table-min": `${minWidth}px` } as React.CSSProperties}
          className="w-full min-w-(--table-min) border-collapse tablet:min-w-0 phone:[&_td]:whitespace-normal"
        >
          <thead>
            <tr>
              {table.columns.map((c) => (
                <HeaderCell key={c.key} table={table} column={c} showPii={showPii} />
              ))}
            </tr>
          </thead>
          <tbody>
            {table.rows.map((row) => (
              <tr key={rowKey(row)} className="border-t border-divider hover:bg-row-hover">
                {table.columns.map((c) => (
                  <td
                    key={c.key}
                    className={cn(
                      "px-3 py-3 align-middle first:pl-6 last:pr-6 phone:first:pl-4",
                      c.align === "right" && "text-right",
                      c.hide && HIDE[c.hide],
                      c.className,
                      c.pii && "px-0",
                    )}
                  >
                    {c.pii ? (
                      <Reveal shown={showPii} width={c.pii}>
                        {c.cell(row)}
                      </Reveal>
                    ) : (
                      c.cell(row)
                    )}
                  </td>
                ))}
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      {table.total === 0 && (
        <div className="px-6 py-8 text-center text-[13px] text-ink-3">{empty}</div>
      )}
      {Number.isFinite(table.perPage) && <Pager table={table} />}
    </>
  );
}

function HeaderCell<R>({
  table,
  column: c,
  showPii,
}: {
  table: TableModel<R>;
  column: Column<R>;
  showPii: boolean;
}) {
  const sorted = table.sort?.key === c.key ? table.sort.dir : null;
  const SortIcon = sorted === "asc" ? ArrowUp : sorted === "desc" ? ArrowDown : ChevronsUpDown;
  const content = (
    <div className="inline-flex items-center gap-0.5">
      {c.sortValue ? (
        <button
          type="button"
          onClick={() => table.toggleSort(c.key)}
          className="-ml-1.5 inline-flex min-h-8 items-center gap-1 rounded-md px-1.5 text-xs font-medium text-ink-3 hover:bg-soft"
        >
          {c.label}
          <SortIcon className={cn("h-[13px] w-[13px]", sorted ? "text-ink" : "text-side-muted")} />
        </button>
      ) : (
        <span>{c.label}</span>
      )}
      {c.filterValue && <FilterMenu table={table} column={c} />}
    </div>
  );
  return (
    <th
      scope="col"
      aria-sort={
        sorted === "asc" ? "ascending" : sorted === "desc" ? "descending" : c.sortValue ? "none" : undefined
      }
      className={cn(
        "whitespace-nowrap px-3 py-1.5 text-left text-xs font-medium text-ink-3 first:pl-6 last:pr-6 phone:first:pl-4",
        c.align === "right" && "text-right",
        c.hide && HIDE[c.hide],
        c.pii && "px-0",
      )}
    >
      {c.pii ? (
        <Reveal shown={showPii} width={c.pii}>
          {content}
        </Reveal>
      ) : (
        content
      )}
    </th>
  );
}

function FilterMenu<R>({ table, column }: { table: TableModel<R>; column: Column<R> }) {
  const chosen = table.filters[column.key] ?? [];
  return (
    <DropdownMenu>
      <DropdownMenuTrigger asChild>
        <button
          type="button"
          aria-label={`Filter ${column.label}`}
          title={`Filter by ${column.label.toLowerCase()}`}
          className="flex size-7 items-center justify-center rounded-md hover:bg-soft"
        >
          <Funnel
            className={cn(
              "h-[13px] w-[13px]",
              chosen.length ? "fill-ink text-ink" : "text-side-muted",
            )}
          />
        </button>
      </DropdownMenuTrigger>
      <DropdownMenuContent
        align="start"
        className="w-[232px] rounded-xl border-line bg-surface p-2.5 text-ink shadow-[0_12px_32px_rgba(20,22,26,0.16)]"
      >
        <DropdownMenuLabel className="px-1.5 pb-2 pt-0.5 text-xs font-semibold normal-case tracking-normal text-ink">
          Filter by {column.label.toLowerCase()}
        </DropdownMenuLabel>
        {table.options(column).map((o) => (
          <DropdownMenuCheckboxItem
            key={o.value}
            checked={o.checked}
            // Keeps the menu open for picking several values.
            onSelect={(e) => e.preventDefault()}
            onCheckedChange={(on) =>
              table.setFilter(
                column.key,
                on ? [...chosen, o.value] : chosen.filter((v) => v !== o.value),
              )
            }
            className="min-h-[34px]"
          >
            <span className="flex-1">{o.value || "—"}</span>
            <span className="text-xs tabular-nums text-ink-3">{o.count}</span>
          </DropdownMenuCheckboxItem>
        ))}
        <DropdownMenuSeparator className="bg-divider" />
        <div className="flex justify-end">
          <DropdownMenuItem
            onSelect={() => table.setFilter(column.key, [])}
            className="min-h-[30px] text-xs text-link focus:text-link-hover"
          >
            Clear
          </DropdownMenuItem>
        </div>
      </DropdownMenuContent>
    </DropdownMenu>
  );
}

function pageNumbers(page: number, pages: number): (number | "gap")[] {
  const out: (number | "gap")[] = [];
  for (let p = 1; p <= pages; p++) {
    if (p === 1 || p === pages || Math.abs(p - page) <= 1) out.push(p);
    else if (out[out.length - 1] !== "gap") out.push("gap");
  }
  return out;
}

function Pager<R>({ table }: { table: TableModel<R> }) {
  const start = (table.page - 1) * table.perPage;
  const nav = "flex size-8 items-center justify-center rounded-lg text-ink-2 hover:bg-soft disabled:opacity-40 disabled:hover:bg-transparent";
  return (
    <div className="flex flex-wrap items-center justify-between gap-3 border-t border-line px-6 py-2.5 text-[13px] text-ink-3 phone:px-4">
      <span className="tabular-nums">
        {table.total
          ? `${start + 1}–${start + table.rows.length} of ${table.total}`
          : "No results"}
      </span>
      <div className="flex items-center gap-0.5">
        <button
          type="button"
          aria-label="Previous page"
          disabled={table.page <= 1}
          onClick={() => table.setPage(table.page - 1)}
          className={nav}
        >
          <ChevronLeft className="h-4 w-4" />
        </button>
        {pageNumbers(table.page, table.pages).map((n, i) =>
          n === "gap" ? (
            <span key={`gap-${i}`} aria-hidden="true" className="min-w-8 text-center">
              …
            </span>
          ) : (
            <button
              key={n}
              type="button"
              aria-current={n === table.page ? "page" : undefined}
              onClick={() => table.setPage(n)}
              className={cn(
                "h-8 min-w-8 rounded-lg px-1.5 text-[13px] tabular-nums",
                n === table.page ? "bg-ink font-medium text-on-ink" : "text-ink-2 hover:bg-soft",
              )}
            >
              {n}
            </button>
          ),
        )}
        <button
          type="button"
          aria-label="Next page"
          disabled={table.page >= table.pages}
          onClick={() => table.setPage(table.page + 1)}
          className={nav}
        >
          <ChevronRight className="h-4 w-4" />
        </button>
      </div>
    </div>
  );
}

/** Collapses its content to zero width while hidden; instant under reduced motion. */
export function Reveal({
  shown,
  width,
  children,
}: {
  shown: boolean;
  width: number;
  children: React.ReactNode;
}) {
  return (
    <div
      inert={!shown}
      style={{ maxWidth: shown ? width : 0, opacity: shown ? 1 : 0 }}
      className="overflow-hidden whitespace-nowrap transition-[max-width,opacity] duration-[260ms] ease-[cubic-bezier(0.2,0.8,0.2,1)] motion-reduce:transition-none"
    >
      <div className="px-3">{children}</div>
    </div>
  );
}

/** The eye button for `showPii`; the state is the page's, hidden on every load. */
export function PiiToggle({ shown, onToggle }: { shown: boolean; onToggle: () => void }) {
  const label = shown ? "Hide student IDs and names" : "Show student IDs and names";
  const Icon = shown ? Eye : EyeOff;
  return (
    <button
      type="button"
      aria-label={label}
      title={label}
      aria-pressed={shown}
      onClick={onToggle}
      className={cn(
        "flex size-9 shrink-0 items-center justify-center rounded-[9px] border transition-colors motion-reduce:transition-none",
        shown ? "border-link/40 bg-badge-info text-link" : "border-field bg-surface text-ink-3",
      )}
    >
      <Icon className="h-[17px] w-[17px]" />
    </button>
  );
}
