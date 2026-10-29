import { cn } from "@/lib/cn";

/** A row of mutually exclusive buttons on a grey track. */
export function Segmented<K extends string>({
  label,
  options,
  value,
  onChange,
  className,
}: {
  label: string;
  options: readonly (readonly [K, string])[];
  value: K;
  onChange: (key: K) => void;
  className?: string;
}) {
  return (
    <div
      role="group"
      aria-label={label}
      className={cn("flex flex-wrap gap-1 rounded-[10px] bg-soft p-1", className)}
    >
      {options.map(([key, text]) => (
        <button
          key={key}
          type="button"
          aria-pressed={value === key}
          onClick={() => onChange(key)}
          className={cn(
            "min-h-8 rounded-[7px] px-3 text-[13px]",
            value === key ? "bg-ink font-medium text-on-ink" : "text-ink hover:bg-surface/70",
          )}
        >
          {text}
        </button>
      ))}
    </div>
  );
}
