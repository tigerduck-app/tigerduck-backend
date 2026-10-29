import { useState } from "react";
import { ArrowDown, ArrowUp, ExternalLink, Minus, Send, type LucideIcon } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Dot, FormDialog, Pill, fieldClass, type Tone } from "@/components/page";
import { AndroidPreview, IphonePreview, PushCard } from "@/components/push-preview";
import { useEnv } from "@/hooks/use-env";
import { cn } from "@/lib/cn";
import { monthDay, monthDayTime } from "@/lib/format";
import type { BulletinDetail, Importance, Taxonomy } from "@/types/api";

export type Draft = {
  title: string;
  title_clean: string;
  summary: string;
  body_clean: string;
  body_md: string;
  canonical_org: string;
  importance: Importance;
  content_tags: string[];
  source_url: string;
};

const MAX_TAGS = 8;

// A scraped or typed URL may carry any scheme; only these open as links.
const isHttpUrl = (url: string) => /^https?:\/\//i.test(url.trim());

export const IMPORTANCE: Record<Importance, { label: string; icon: LucideIcon; tone: Tone; className?: string }> = {
  low: { label: "Low", icon: ArrowDown, tone: "neutral", className: "bg-ground text-ink-3" },
  normal: { label: "Normal", icon: Minus, tone: "neutral" },
  high: { label: "High", icon: ArrowUp, tone: "orange" },
};

function draftOf(b: BulletinDetail | null, taxonomy: Taxonomy): Draft {
  return {
    title: b?.title ?? "",
    title_clean: b?.title_clean ?? "",
    summary: b?.summary ?? "",
    body_clean: b?.body_clean ?? "",
    body_md: b?.body_md ?? "",
    canonical_org: b?.canonical_org ?? taxonomy.orgs[0]?.id ?? "",
    importance: b?.importance ?? "normal",
    content_tags: b?.content_tags ?? [],
    source_url: b?.source_url ?? "https://announce.ntust.edu.tw/manual",
  };
}

/** The bulletin's push title and body, as `server/bulletins/user_dispatch.py` builds them. */
export function pushText(d: Pick<Draft, "title" | "title_clean" | "summary">) {
  const title = d.title_clean.trim() || d.title.trim();
  return { title: title || "Title", body: d.summary.trim() || title || "Summary" };
}

function Counted({ label, optional, count, max, children }: {
  label: string;
  optional?: boolean;
  count: number;
  max: number;
  children: React.ReactNode;
}) {
  return (
    <label className="flex flex-col gap-1.5 text-[13px] font-medium">
      <span className="flex justify-between gap-2">
        <span>
          {label} {optional && <span className="font-normal text-ink-3">optional</span>}
        </span>
        <span className="font-normal tabular-nums text-ink-3">
          {count} / {max}
        </span>
      </span>
      {children}
    </label>
  );
}

const textareaClass =
  "w-full resize-y rounded-[9px] border border-field bg-surface px-3 py-2.5 text-sm font-normal text-ink";

function segment(on: boolean) {
  return cn(
    "flex min-h-8 flex-1 items-center justify-center gap-1.5 rounded-[7px] px-3 text-[13px]",
    on ? "bg-surface font-medium text-ink shadow-[0_1px_2px_rgba(20,22,26,0.12)]" : "text-ink-2",
  );
}

/**
 * A new bulletin when `bulletin` is null, otherwise edits it; either way the
 * operator reviews it in a confirm step before `onSubmit`.
 */
export function AnnouncementDialog({
  bulletin,
  taxonomy,
  pending,
  onClose,
  onSubmit,
}: {
  bulletin: BulletinDetail | null;
  taxonomy: Taxonomy;
  pending: boolean;
  onClose: () => void;
  onSubmit: (draft: Draft) => void;
}) {
  const [d, setD] = useState(() => draftOf(bulletin, taxonomy));
  const [step, setStep] = useState<"compose" | "confirm">("compose");
  const [bodyTab, setBodyTab] = useState(d.body_md && !d.body_clean ? "md" : "plain");
  const env = useEnv().data?.env;

  const set = (patch: Partial<Draft>) => setD((cur) => ({ ...cur, ...patch }));
  const field = (key: keyof Draft) => (e: React.ChangeEvent<HTMLInputElement | HTMLTextAreaElement | HTMLSelectElement>) =>
    set({ [key]: e.target.value });

  const isEdit = bulletin !== null;
  const push = pushText(d);
  const orgLabel = taxonomy.orgs.find((o) => o.id === d.canonical_org)?.label ?? d.canonical_org;
  const tagLabel = (id: string) => taxonomy.tags.find((t) => t.id === id)?.label ?? id;
  const full = d.content_tags.length >= MAX_TAGS;
  const imp = IMPORTANCE[d.importance];
  // An edit sends no source URL; a new one must be http or https, as the server checks.
  const canSubmit = d.title.trim().length > 0 && (isEdit || isHttpUrl(d.source_url));

  const chosenTags = d.content_tags.map((t) => (
    <span key={t} className="rounded-full border border-line bg-ground px-2 py-px text-xs text-ink-2">
      {tagLabel(t)}
    </span>
  ));
  const inApp = (
    <div className="flex flex-col gap-2 rounded-[14px] border border-line bg-surface p-3.5">
      <div className="flex flex-wrap items-center gap-1.5">
        <Pill tone="info">{orgLabel}</Pill>
        <Pill tone={imp.tone} className={imp.className}>
          {imp.label}
        </Pill>
        <span className="ml-auto text-xs text-ink-3">
          {isEdit && bulletin.posted_at ? monthDay(bulletin.posted_at) : "Today"}
        </span>
      </div>
      <span className="break-words text-[15px] font-semibold">{push.title}</span>
      {d.summary.trim() && <span className="break-words text-[13px] text-ink-2">{d.summary}</span>}
      {chosenTags.length > 0 && <div className="flex flex-wrap gap-1">{chosenTags}</div>}
    </div>
  );

  if (step === "confirm") {
    return (
      <FormDialog
        open
        onOpenChange={(open) => !open && onClose()}
        title={isEdit ? `Save bulletin #${bulletin.id}?` : "Broadcast announcement?"}
        meta={
          env && (
            <span className="inline-flex items-center gap-1.5 text-ink-2">
              <Dot tone={env === "production" ? "warn" : "ok"} />
              {env.charAt(0).toUpperCase() + env.slice(1)}
            </span>
          )
        }
        width={560}
        footer={
          <>
            <Button variant="line" size="form" onClick={() => setStep("compose")}>
              Back
            </Button>
            <Button variant="danger" size="form" disabled={pending} onClick={() => onSubmit(d)}>
              {isEdit ? "Save changes" : "Broadcast"}
            </Button>
          </>
        }
      >
        {/* An edit is saved without a push, so it shows the bulletin as the app lists it. */}
        {isEdit ? (
          inApp
        ) : (
          <PushCard title={push.title} body={push.body}>
            <Pill tone="info">{orgLabel}</Pill>
            {chosenTags}
          </PushCard>
        )}
      </FormDialog>
    );
  }

  return (
    <FormDialog
      open
      onOpenChange={(open) => !open && onClose()}
      title={isEdit ? `Edit bulletin #${bulletin.id}` : "New announcement"}
      meta={
        isEdit
          ? `${bulletin.source} · Posted ${bulletin.posted_at ? monthDayTime(bulletin.posted_at) : "—"}`
          : undefined
      }
      width={1040}
      footer={
        <>
          <Button variant="line" size="form" onClick={onClose}>
            Cancel
          </Button>
          <Button variant="ink" size="form" disabled={!canSubmit} onClick={() => setStep("confirm")}>
            <Send />
            {isEdit ? "Save changes" : "Broadcast"}
          </Button>
        </>
      }
    >
      <div className="flex flex-wrap gap-6">
        <div className="flex min-w-0 flex-[1_1_520px] flex-col gap-4">
          <Counted label="Title" count={d.title.length} max={500}>
            <input value={d.title} onChange={field("title")} maxLength={500} required autoFocus className={cn(fieldClass, "font-normal")} />
          </Counted>
          <Counted label="Title (clean)" optional count={d.title_clean.length} max={200}>
            <input
              value={d.title_clean}
              onChange={field("title_clean")}
              placeholder={d.title}
              maxLength={200}
              className={cn(fieldClass, "font-normal")}
            />
          </Counted>
          <Counted label="Summary" optional count={d.summary.length} max={2000}>
            <textarea rows={2} value={d.summary} onChange={field("summary")} maxLength={2000} className={textareaClass} />
          </Counted>

          <div className="flex flex-wrap gap-4">
            <label className="flex flex-[1_1_220px] flex-col gap-1.5 text-[13px] font-medium">
              Organization
              <select value={d.canonical_org} onChange={field("canonical_org")} className={cn(fieldClass, "font-normal")}>
                {taxonomy.orgs.map((o) => (
                  <option key={o.id} value={o.id}>
                    {o.label}
                  </option>
                ))}
              </select>
            </label>
            <div className="flex flex-[1_1_240px] flex-col gap-1.5">
              <span id="imp-label" className="text-[13px] font-medium">
                Importance
              </span>
              <div role="radiogroup" aria-labelledby="imp-label" className="flex gap-1 rounded-[10px] bg-soft p-1">
                {(Object.keys(IMPORTANCE) as Importance[]).map((k) => {
                  const { label, icon: Icon } = IMPORTANCE[k];
                  return (
                    <button
                      key={k}
                      type="button"
                      role="radio"
                      aria-checked={d.importance === k}
                      onClick={() => set({ importance: k })}
                      className={segment(d.importance === k)}
                    >
                      <Icon aria-hidden="true" className="size-3" strokeWidth={2.4} />
                      {label}
                    </button>
                  );
                })}
              </div>
            </div>
          </div>

          <div className="flex flex-col gap-2">
            <span className="flex justify-between text-[13px] font-medium">
              Tags
              <span className="font-normal tabular-nums text-ink-3">
                {d.content_tags.length} / {MAX_TAGS}
              </span>
            </span>
            <div role="group" aria-label="Tags" className="flex flex-wrap gap-1.5">
              {taxonomy.tags.map((t) => {
                const on = d.content_tags.includes(t.id);
                return (
                  <button
                    key={t.id}
                    type="button"
                    aria-pressed={on}
                    disabled={!on && full}
                    onClick={() =>
                      set({
                        content_tags: on ? d.content_tags.filter((x) => x !== t.id) : [...d.content_tags, t.id],
                      })
                    }
                    className={cn(
                      "min-h-8 rounded-full border px-3 text-[13px] disabled:cursor-not-allowed disabled:text-side-muted",
                      on ? "border-ink bg-ink text-on-ink" : "border-field bg-surface text-ink hover:bg-row-hover",
                    )}
                  >
                    {t.label}
                  </button>
                );
              })}
            </div>
          </div>

          <div className="flex flex-col gap-2">
            <div className="flex items-center justify-between gap-2">
              <span className="text-[13px] font-medium">Body</span>
              <div role="tablist" aria-label="Body format" className="flex gap-0.5 rounded-[9px] bg-soft p-[3px]">
                {(["plain", "md"] as const).map((tab) => (
                  <button
                    key={tab}
                    type="button"
                    role="tab"
                    aria-selected={bodyTab === tab}
                    onClick={() => setBodyTab(tab)}
                    className={cn(segment(bodyTab === tab), "min-h-7")}
                  >
                    {tab === "plain" ? "Plain" : "Markdown"}
                  </button>
                ))}
              </div>
            </div>
            {bodyTab === "plain" ? (
              <textarea
                aria-label="Body (plain)"
                rows={5}
                value={d.body_clean}
                onChange={field("body_clean")}
                maxLength={20000}
                className={textareaClass}
              />
            ) : (
              <textarea
                aria-label="Body (markdown)"
                rows={5}
                value={d.body_md}
                onChange={field("body_md")}
                maxLength={20000}
                className={cn(textareaClass, "font-mono text-[13px]")}
              />
            )}
          </div>

          {isEdit ? (
            <div className="flex flex-col gap-1.5 text-[13px]">
              <span className="font-medium">Source URL</span>
              {isHttpUrl(d.source_url) ? (
                <a
                  href={d.source_url}
                  target="_blank"
                  rel="noreferrer noopener"
                  className="inline-flex min-w-0 items-center gap-1.5 font-mono text-link hover:text-link-hover"
                >
                  <span className="truncate">{d.source_url}</span>
                  <ExternalLink aria-hidden="true" className="size-3.5 shrink-0" />
                </a>
              ) : (
                <span className="break-all font-mono text-ink-2">{d.source_url || "—"}</span>
              )}
            </div>
          ) : (
            <label className="flex flex-col gap-1.5 text-[13px] font-medium">
              Source URL
              <input
                type="url"
                aria-invalid={!isHttpUrl(d.source_url)}
                value={d.source_url}
                onChange={field("source_url")}
                maxLength={1000}
                className={cn(fieldClass, "font-mono text-[13px] font-normal", !isHttpUrl(d.source_url) && "border-dot-bad")}
              />
            </label>
          )}
        </div>

        <aside
          aria-label="Preview"
          className="flex min-w-[280px] flex-[0_1_360px] flex-col gap-3.5 self-start rounded-[14px] bg-ground p-4 max-[720px]:min-w-0 max-[720px]:basis-full"
        >
          <span className="text-xs font-semibold uppercase tracking-[0.06em] text-ink-3">Preview</span>
          <IphonePreview title={push.title} body={push.body} className="max-[720px]:hidden" />
          <AndroidPreview title={push.title} body={push.body} className="max-[720px]:hidden" />
          <div className="flex flex-col gap-1.5">
            <span className="text-xs text-ink-3">In the app</span>
            {inApp}
          </div>
        </aside>
      </div>
    </FormDialog>
  );
}
