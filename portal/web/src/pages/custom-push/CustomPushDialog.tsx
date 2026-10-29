import { useEffect, useState } from "react";
import { keepPreviousData, useQuery } from "@tanstack/react-query";
import { Bell, BellOff, BookOpen, ChevronDown, ChevronUp, Users } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Dot, ErrorAlert, FormDialog, Pill, Switch, fieldClass } from "@/components/page";
import { AndroidPreview, IphonePreview, PushCard } from "@/components/push-preview";
import { useEnv } from "@/hooks/use-env";
import { api, errorText } from "@/lib/api";
import { cn } from "@/lib/cn";
import type {
  CustomPushPreviewResponse,
  CustomPushRequest,
  CustomPushTargetClass,
  DeviceList,
} from "@/types/api";
import { ALL_CLASSES, CLASSES, ClassIcon, KINDS, count } from "./kinds";

type Kind = keyof typeof KINDS;
type Row = { label: string; signed: number; anon: number };

/** "1151" -> "115-1", as both apps show a term. */
const termLabel = (code: string) => (code.length === 4 ? `${code.slice(0, 3)}-${code.slice(3)}` : code);
const fmt = (n: number) => n.toLocaleString("en-US");

function useDebounced<T>(value: T, ms: number): T {
  const [settled, setSettled] = useState(value);
  useEffect(() => {
    const t = setTimeout(() => setSettled(value), ms);
    return () => clearTimeout(t);
  }, [value, ms]);
  return settled;
}

function usePreview(classes: CustomPushTargetClass[], filter: string) {
  return useQuery<CustomPushPreviewResponse>({
    queryKey: ["custom-push-preview", classes, filter],
    queryFn: () =>
      api("/api/custom-push/preview", {
        method: "POST",
        json: { ...JSON.parse(filter), target_classes: classes },
      }),
    enabled: classes.length > 0,
    placeholderData: keepPreviousData,
  });
}

/** Each selected class, then any device the server could not place in one. */
function rowsOf(p: CustomPushPreviewResponse, classes: CustomPushTargetClass[]): Row[] {
  const known = CLASSES.filter((c) => classes.includes(c.id)).map((c) => [c.id as string, c.label]);
  const unplaced = Object.keys(p.matched)
    .filter((k) => k !== "total" && !ALL_CLASSES.includes(k as CustomPushTargetClass))
    .map((k) => [k, k]);
  return [...known, ...unplaced].map(([key, label]) => {
    const anon = p.anonymous[key] ?? 0;
    return { label, signed: (p.matched[key] ?? 0) - anon, anon };
  });
}

function Bars({ rows }: { rows: Row[] }) {
  const max = Math.max(1, ...rows.map((r) => r.signed + r.anon));
  return rows.map((r) => (
    <div key={r.label} className="flex items-center gap-3 text-[13px]">
      <span className="w-[104px] shrink-0 truncate">{r.label}</span>
      <div className="flex h-2.5 flex-1 gap-0.5">
        {r.signed > 0 && (
          <span
            title={`Signed in ${fmt(r.signed)}`}
            style={{ width: `${(r.signed / max) * 100}%` }}
            className={cn("bg-chart-1", r.anon ? "rounded-l" : "rounded")}
          />
        )}
        {r.anon > 0 && (
          <span
            title={`Not signed in ${fmt(r.anon)}`}
            style={{ width: `${(r.anon / max) * 100}%` }}
            className={cn("bg-chart-2", r.signed ? "rounded-r" : "rounded")}
          />
        )}
      </div>
      <span className="w-14 shrink-0 text-right tabular-nums">{fmt(r.signed + r.anon)}</span>
    </div>
  ));
}

function Legend() {
  return (
    <div className="flex gap-3.5 text-xs text-ink-2">
      <span className="inline-flex items-center gap-1.5">
        <span aria-hidden="true" className="size-2.5 rounded-[2px] bg-chart-1" />
        Signed in
      </span>
      <span className="inline-flex items-center gap-1.5">
        <span aria-hidden="true" className="size-2.5 rounded-[2px] bg-chart-2" />
        Not signed in
      </span>
    </div>
  );
}

const legendClass = "text-xs font-semibold uppercase tracking-[0.06em] text-ink-3";
const inputClass = (invalid: boolean, mono = false) =>
  cn(fieldClass, "font-normal", mono && "font-mono text-[13px]", invalid && "border-dot-bad");

/** Composes a custom push and, on review, asks to confirm before `onSend`. */
export function CustomPushDialog({
  listId: initialListId = "",
  pending,
  onClose,
  onSend,
}: {
  /** The device list the composer starts targeting. */
  listId?: string;
  pending: boolean;
  onClose: () => void;
  onSend: (request: CustomPushRequest) => void;
}) {
  const [title, setTitle] = useState("");
  const [body, setBody] = useState("");
  const [chosenKind, setKind] = useState<Kind>("record");
  const [sound, setSound] = useState(true);
  const [classes, setClasses] = useState<CustomPushTargetClass[]>(ALL_CLASSES);
  const [listId, setListId] = useState(initialListId);
  const [courseCode, setCourseCode] = useState("");
  const [userId, setUserId] = useState("");
  const [deviceId, setDeviceId] = useState("");
  const [advanced, setAdvanced] = useState(false);
  const [touched, setTouched] = useState(false);
  const [step, setStep] = useState<"compose" | "confirm">("compose");
  const env = useEnv().data?.env;

  const listsQ = useQuery<DeviceList[]>({
    queryKey: ["device-lists"],
    queryFn: () => api<DeviceList[]>("/api/device-lists"),
  });

  const code = courseCode.trim().toUpperCase() || undefined;
  // A course push cannot keep a record: it would land in every user's feed.
  const kind: Kind = code ? "popup" : chosenKind;
  const filter = {
    keeps_record: kind === "record",
    list_id: listId ? Number(listId) : undefined,
    course_code: code,
    user_id: userId.trim() || undefined,
    device_id: deviceId.trim() || undefined,
  };
  const filterKey = JSON.stringify(filter);
  const settledKey = useDebounced(filterKey, 300);
  const selQ = usePreview(classes, settledKey);
  const allQ = usePreview(ALL_CLASSES, settledKey);
  const fresh = (q: typeof selQ) => (settledKey === filterKey && !q.isPlaceholderData ? q.data : undefined);
  const sel = classes.length ? fresh(selQ) : undefined;
  const all = fresh(allQ);
  // A failed count is shown as the failure, never as an audience of zero.
  const failed = settledKey !== filterKey ? null : [classes.length ? selQ : null, allQ].find((q) => q?.isError);

  const term = (all ?? allQ.data)?.term ?? null;
  const course = all?.course ?? null;
  const courseMiss = !!filter.course_code && !!all && !course;
  const total = sel?.matched.total ?? 0;
  const rows = sel ? rowsOf(sel, classes) : [];
  const listName = listsQ.data?.find((l) => String(l.id) === listId)?.name;

  const titleErr = touched && !title.trim();
  const bodyErr = touched && !body.trim();
  const classErr = touched && classes.length === 0;
  const valid = !!title.trim() && !!body.trim() && total > 0 && !courseMiss;
  const kindLabel = KINDS[kind].label;
  const preview = { title: title.trim() || "Title", body: body.trim() || "Body" };

  const tileCount = (c: CustomPushTargetClass) => {
    const n = (classes.includes(c) ? sel : all)?.matched[c];
    return n === undefined ? "—" : fmt(n);
  };

  if (step === "confirm") {
    const target = [
      listName,
      course && `${course.code} ${course.name}`,
      filter.user_id,
      filter.device_id?.slice(0, 8),
    ]
      .filter(Boolean)
      .join(" · ");
    return (
      <FormDialog
        open
        onOpenChange={(open) => !open && onClose()}
        title={`Send to ${count(total, "device")}?`}
        meta={
          env && (
            <span className="inline-flex items-center gap-1.5 text-ink-2">
              <Dot tone={env === "production" ? "warn" : "ok"} />
              {env.charAt(0).toUpperCase() + env.slice(1)} · sends at once
            </span>
          )
        }
        width={560}
        footer={
          <>
            <Button variant="line" size="form" onClick={() => setStep("compose")}>
              Back
            </Button>
            <Button
              variant="danger"
              size="form"
              disabled={pending}
              onClick={() =>
                onSend({
                  title,
                  body,
                  keeps_record: filter.keeps_record,
                  force_ring: sound,
                  target_classes: classes,
                  list_id: filter.list_id,
                  course_code: filter.course_code,
                  user_id: filter.user_id,
                  device_id: filter.device_id,
                })
              }
            >
              Send to {fmt(total)}
            </Button>
          </>
        }
      >
        <PushCard title={title} body={body}>
          <Pill tone={KINDS[kind].tone}>{kindLabel}</Pill>
          <Pill>{sound ? "Sound" : "Silent"}</Pill>
          {target && <Pill className="whitespace-normal break-all font-mono">{target}</Pill>}
        </PushCard>
        <div className="flex flex-col gap-2">
          <div className="flex flex-wrap items-center justify-between gap-2">
            <span className={legendClass}>Audience</span>
            <Legend />
          </div>
          <Bars rows={rows} />
        </div>
      </FormDialog>
    );
  }

  return (
    <FormDialog
      open
      onOpenChange={(open) => !open && onClose()}
      title="New custom push"
      width={1080}
      footer={
        <>
          <span className="mr-auto flex items-center gap-1.5 self-center text-[13px] text-ink-2 max-[720px]:mr-0 max-[720px]:basis-full">
            <Users aria-hidden="true" className="size-[15px]" />
            {[sel && count(total, "device"), kindLabel, sound ? "Sound" : "Silent"].filter(Boolean).join(" · ")}
          </span>
          <Button variant="line" size="form" onClick={onClose} className="max-[720px]:flex-1">
            Cancel
          </Button>
          <Button
            variant="ink"
            size="form"
            aria-disabled={!valid}
            onClick={() => (valid ? setStep("confirm") : setTouched(true))}
            className={cn("max-[720px]:flex-1", !valid && "bg-soft text-ink-3 hover:bg-soft")}
          >
            Review and send
          </Button>
        </>
      }
    >
      <div className="flex flex-wrap gap-6">
        <div className="flex min-w-0 flex-[1_1_560px] flex-col gap-[22px]">
          <fieldset className="flex flex-col gap-3">
            <legend className={cn(legendClass, "pb-2.5")}>Message</legend>
            <label className="flex flex-col gap-1.5 text-[13px] font-medium">
              <span className="flex justify-between">
                Title
                <span className="font-normal tabular-nums text-ink-3">{title.length} / 500</span>
              </span>
              <input
                value={title}
                onChange={(e) => setTitle(e.target.value)}
                maxLength={500}
                aria-invalid={titleErr}
                autoFocus
                className={inputClass(titleErr)}
              />
              {titleErr && <span className="text-xs text-badge-danger-fg">Required</span>}
            </label>
            <label className="flex flex-col gap-1.5 text-[13px] font-medium">
              <span className="flex justify-between">
                Body
                <span className="font-normal tabular-nums text-ink-3">{body.length} / 2000</span>
              </span>
              <textarea
                rows={3}
                value={body}
                onChange={(e) => setBody(e.target.value)}
                maxLength={2000}
                aria-invalid={bodyErr}
                className={cn(inputClass(bodyErr), "h-auto resize-y py-2.5")}
              />
              {bodyErr && <span className="text-xs text-badge-danger-fg">Required</span>}
            </label>
          </fieldset>

          <fieldset className="flex flex-col gap-3">
            <legend className={cn(legendClass, "pb-2.5")}>Delivery</legend>
            <div role="radiogroup" aria-label="Kind" className="flex flex-wrap gap-2">
              {(Object.keys(KINDS) as Kind[]).map((k) => {
                const { label, note, icon: Icon } = KINDS[k];
                const on = kind === k;
                const locked = k === "record" && !!code;
                return (
                  <button
                    key={k}
                    type="button"
                    role="radio"
                    aria-checked={on}
                    disabled={locked}
                    onClick={() => setKind(k)}
                    className={cn(
                      "flex min-h-[60px] flex-[1_1_220px] items-center gap-3 rounded-xl border-2 px-3.5 py-2.5 text-left disabled:cursor-not-allowed disabled:opacity-50",
                      !on && "border-line bg-surface",
                      on && k === "record" && "border-badge-info-fg bg-badge-info",
                      on && k === "popup" && "border-badge-violet-fg bg-badge-violet",
                    )}
                  >
                    <Icon
                      aria-hidden="true"
                      className={cn("size-5 shrink-0", k === "record" ? "text-badge-info-fg" : "text-badge-violet-fg")}
                    />
                    <span className="flex flex-col gap-px">
                      <span className="font-semibold">{label}</span>
                      <span className="text-xs text-ink-3">{note}</span>
                    </span>
                  </button>
                );
              })}
            </div>
            <div className="flex min-h-10 items-center justify-between gap-3">
              <span className="flex items-center gap-2">
                {sound ? <Bell aria-hidden="true" className="size-4" /> : <BellOff aria-hidden="true" className="size-4" />}
                Play sound
              </span>
              <Switch checked={sound} onCheckedChange={setSound} label="Play sound" />
            </div>
          </fieldset>

          <fieldset className="flex flex-col gap-3">
            <legend className={cn(legendClass, "pb-2.5")}>Audience</legend>
            <div role="group" aria-label="Device classes" className="flex flex-wrap gap-2">
              {CLASSES.map((c) => {
                const on = classes.includes(c.id);
                return (
                  <button
                    key={c.id}
                    type="button"
                    aria-pressed={on}
                    onClick={() =>
                      setClasses(on ? classes.filter((x) => x !== c.id) : ALL_CLASSES.filter((x) => x === c.id || classes.includes(x)))
                    }
                    className={cn(
                      "flex min-h-[84px] flex-[1_1_110px] flex-col items-start justify-between gap-1 rounded-xl border-2 px-3 py-2.5 text-left max-[720px]:min-h-12 max-[720px]:basis-[140px] max-[720px]:flex-row max-[720px]:items-center max-[720px]:justify-start max-[720px]:gap-2",
                      on ? "border-ink bg-surface text-ink" : "border-dashed border-field bg-row-hover text-ink-3",
                    )}
                  >
                    <ClassIcon d={c.d} className="size-[18px] shrink-0" />
                    <span className="text-[13px] font-medium">{c.label}</span>
                    <span className={cn("text-xs tabular-nums max-[720px]:ml-auto", on ? "text-ink-2" : "text-ink-3")}>
                      {tileCount(c.id)}
                    </span>
                  </button>
                );
              })}
            </div>
            {classErr && <span className="text-xs text-badge-danger-fg">Pick at least one device class</span>}

            <div className="flex flex-wrap items-end gap-3 max-[720px]:flex-col max-[720px]:items-stretch">
              <label className="flex flex-[1_1_260px] flex-col gap-1.5 text-[13px] font-medium max-[720px]:flex-none">
                List
                <select value={listId} onChange={(e) => setListId(e.target.value)} className={inputClass(false)}>
                  <option value="">No list</option>
                  {listsQ.data?.map((l) => (
                    <option key={l.id} value={l.id}>
                      {l.name} · {l.member_count}
                    </option>
                  ))}
                </select>
              </label>
              {term && (
                <label className="flex flex-[1_1_220px] flex-col gap-1.5 text-[13px] font-medium max-[720px]:flex-none">
                  Course in {termLabel(term)}
                  <input
                    value={courseCode}
                    onChange={(e) => setCourseCode(e.target.value)}
                    placeholder="CS2001301"
                    maxLength={16}
                    aria-invalid={courseMiss}
                    className={cn(inputClass(courseMiss, true), "uppercase placeholder:normal-case")}
                  />
                </label>
              )}
              <Button
                variant="line"
                size="form"
                aria-expanded={advanced}
                onClick={() => setAdvanced(!advanced)}
                className="font-normal"
              >
                More filters
                {advanced ? <ChevronUp /> : <ChevronDown />}
              </Button>
            </div>
            {course && (
              <div className="inline-flex flex-wrap items-center gap-2.5 self-start rounded-[10px] border border-badge-info-fg/25 bg-badge-info px-3 py-2 text-[13px]">
                <BookOpen aria-hidden="true" className="size-4 text-badge-info-fg" />
                <span className="font-mono font-medium">{course.code}</span>
                <span className="font-semibold">{course.name}</span>
                <span className="tabular-nums text-ink-2">
                  {count(course.students, "student")} · {sel ? count(total, "device") : "—"}
                </span>
              </div>
            )}
            {courseMiss && term && (
              <span role="alert" className="text-xs font-medium text-badge-danger-fg">
                No student takes {filter.course_code} in {termLabel(term)}
              </span>
            )}
            {advanced && (
              <div className="flex flex-wrap gap-3">
                <label className="flex flex-[1_1_220px] flex-col gap-1.5 text-[13px] font-medium">
                  Student ID
                  <input
                    value={userId}
                    onChange={(e) => setUserId(e.target.value)}
                    placeholder="B999xxxxx"
                    maxLength={64}
                    className={inputClass(false, true)}
                  />
                </label>
                <label className="flex flex-[1_1_220px] flex-col gap-1.5 text-[13px] font-medium">
                  Device ID
                  <input
                    value={deviceId}
                    onChange={(e) => setDeviceId(e.target.value)}
                    placeholder="UUID"
                    maxLength={128}
                    className={inputClass(false, true)}
                  />
                </label>
              </div>
            )}

            {failed ? (
              <ErrorAlert
                className="items-center"
                onRetry={() => {
                  selQ.refetch();
                  allQ.refetch();
                }}
              >
                {errorText(failed.error)}
              </ErrorAlert>
            ) : (
              <div className="flex flex-col gap-2.5 rounded-xl border border-line px-4 py-3.5">
                <div className="flex flex-wrap items-baseline justify-between gap-2">
                  <div className="flex items-baseline gap-2">
                    <span className="text-2xl font-semibold tabular-nums">{sel ? fmt(total) : "—"}</span>
                    <span className="text-ink-2">devices</span>
                  </div>
                  <Legend />
                </div>
                <Bars rows={rows} />
                {kind === "popup" && (
                  <span className="text-xs text-ink-3">Popup reaches signed-in devices only.</span>
                )}
              </div>
            )}
          </fieldset>
        </div>

        <aside
          aria-label="Preview"
          className="flex min-w-[280px] flex-[0_1_360px] flex-col gap-3.5 self-start rounded-[14px] bg-ground p-4 max-[720px]:min-w-0 max-[720px]:basis-full"
        >
          <span className={legendClass}>Preview</span>
          <IphonePreview title={preview.title} body={preview.body} sound={sound} />
          <AndroidPreview title={preview.title} body={preview.body} className="max-[720px]:hidden" />
          {kind === "record" && (
            <div className="flex flex-col gap-1.5 max-[720px]:hidden">
              <span className="text-xs text-ink-3">Announcement tab</span>
              <div className="flex flex-col gap-1.5 rounded-[14px] border border-line bg-surface p-3.5">
                <div className="flex items-center gap-1.5">
                  <Pill tone="info">伺服器</Pill>
                  <span className="ml-auto text-xs text-ink-3">Today</span>
                </div>
                <span className="break-words text-[15px] font-semibold">{preview.title}</span>
                <span className="break-words text-[13px] text-ink-2">{preview.body}</span>
              </div>
            </div>
          )}
        </aside>
      </div>
    </FormDialog>
  );
}
