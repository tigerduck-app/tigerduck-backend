import { useId, useRef, useState } from "react";
import { useMutation } from "@tanstack/react-query";
import { toast } from "sonner";
import {
  ArrowRight,
  CircleCheck,
  Copy,
  Database,
  Download,
  FileArchive,
  FileText,
  Lock,
  ShieldCheck,
  Upload,
  X,
} from "lucide-react";
import { Button } from "@/components/ui/button";
import { ErrorAlert, FormDialog, PageHeading } from "@/components/page";
import { useEnv } from "@/hooks/use-env";
import { ApiError } from "@/lib/api";
import { cn } from "@/lib/cn";
import { fileSize, monthDayTime } from "@/lib/format";

const RESTART = "./stop.sh && ./start.sh";
const STEPS = ["Dump the live database", "Restore the file", "Restart the stack"];
type StepState = "todo" | "doing" | "done";

function fileMeta(f: File): string {
  const kind = /\.dump$/i.test(f.name) ? "pg_dump" : "Portal export";
  return `${kind} · ${fileSize(f.size)} · ${monthDayTime(new Date(f.lastModified).toISOString())}`;
}

export function BackupPage() {
  const env = useEnv();
  const [file, setFile] = useState<File | null>(null);
  const [confirming, setConfirming] = useState(false);
  const [restoredAt, setRestoredAt] = useState<string | null>(null);
  const fileRef = useRef<HTMLInputElement>(null);

  const exportMut = useMutation({
    mutationFn: async () => {
      const res = await fetch("/api/backup/export", { method: "POST" });
      if (!res.ok) {
        const body = await res.text();
        throw new Error(body || `HTTP ${res.status}`);
      }
      const disposition = res.headers.get("content-disposition") ?? "";
      const match = disposition.match(/filename="?([^";]+)"?/);
      const filename = match?.[1] ?? "tigerduck-export.tar.gz";
      const blob = await res.blob();
      // The browser-side click trick is the standard way to push a Blob
      // out as a download — keeps the actual streaming on fetch() and
      // avoids any anchor-rewrite middlebox surprises.
      const url = URL.createObjectURL(blob);
      const a = document.createElement("a");
      a.href = url;
      a.download = filename;
      document.body.appendChild(a);
      a.click();
      a.remove();
      URL.revokeObjectURL(url);
      return filename;
    },
    onSuccess: (name) => toast.success(`Downloaded ${name}`),
    onError: (e) => toast.error((e as Error).message),
  });

  const importMut = useMutation({
    mutationFn: async (f: File) => {
      const fd = new FormData();
      fd.append("file", f);
      const res = await fetch("/api/backup/import", {
        method: "POST",
        body: fd,
      });
      const body = await res.json().catch(() => ({}));
      if (!res.ok) {
        throw new ApiError(
          res.status,
          (body as { detail?: string }).detail || `HTTP ${res.status}`,
          body,
        );
      }
      return body as { imported_at: string };
    },
    onSuccess: (body) => {
      setRestoredAt(body.imported_at);
      setFile(null);
      if (fileRef.current) fileRef.current.value = "";
      toast.success("Import complete");
    },
  });

  const pick = (f: File | undefined) => {
    if (!f) return;
    importMut.reset();
    setRestoredAt(null);
    setFile(f);
  };
  const clearFile = () => {
    importMut.reset();
    setFile(null);
    if (fileRef.current) fileRef.current.value = "";
  };

  const restoring = importMut.isPending;
  const steps: StepState[] = restoredAt
    ? ["done", "done", "doing"]
    : restoring
      ? ["doing", "doing", "todo"]
      : ["todo", "todo", "todo"];
  const envName = env.data?.env ?? "";

  return (
    <>
      <PageHeading
        group="System"
        title="Backup"
        meta="tigerduck database · pg_dump --format=custom"
      />

      {restoredAt && (
        <div
          role="status"
          className="flex flex-wrap items-center gap-4 rounded-[14px] border border-dot-ok/30 bg-badge-success px-5 py-4"
        >
          <CircleCheck className="h-5 w-5 shrink-0 text-badge-success-fg" />
          <div className="flex flex-[1_1_240px] flex-col gap-0.5 text-badge-success-fg">
            <span className="font-semibold">Restored · {monthDayTime(restoredAt)}</span>
            <span className="text-[13px]">Restart the stack so the backend reads the restored data</span>
          </div>
          <div className="flex items-center gap-1 rounded-[10px] border border-dot-ok/30 bg-surface py-1.5 pl-3 pr-1.5">
            <code className="font-mono text-[13px]">{RESTART}</code>
            <Button
              variant="quiet"
              aria-label="Copy command"
              className="size-8 rounded-lg p-0"
              onClick={() =>
                navigator.clipboard.writeText(RESTART).then(
                  () => toast.success("Copied"),
                  () => toast.error("Copy failed"),
                )
              }
            >
              <Copy />
            </Button>
          </div>
        </div>
      )}

      <div className="flex flex-wrap items-stretch gap-6">
        <Card icon={Download} title="Export">
          <div className="flex flex-wrap items-center gap-2.5">
            <div className="flex flex-col gap-1.5">
              <Chip icon={Database}>pg_dump</Chip>
              <Chip icon={FileText}>manifest.json</Chip>
            </div>
            <ArrowRight aria-hidden="true" className="h-4 w-4 text-ink-3" />
            <span className="inline-flex items-center gap-2 break-all rounded-[10px] bg-badge-info px-3.5 py-2.5 font-mono text-xs font-medium text-badge-info-fg">
              <FileArchive className="h-4 w-4 shrink-0" />
              tigerduck-export-&lt;timestamp&gt;.tar.gz
            </span>
          </div>
          <div className="flex flex-col gap-2">
            <span className="text-xs font-medium text-ink-3">Stays on the server</span>
            <div className="flex flex-wrap gap-1.5">
              {["APNs .p8", "FCM JSON", ".env"].map((s) => (
                <span
                  key={s}
                  className="inline-flex min-h-7 items-center gap-1.5 rounded-full bg-ground px-2.5 text-xs text-ink-2"
                >
                  <Lock className="h-3 w-3" />
                  {s}
                </span>
              ))}
            </div>
          </div>
          <div className="mt-auto">
            <Button
              variant="ink"
              size="form"
              disabled={exportMut.isPending}
              onClick={() => exportMut.mutate()}
              className="disabled:bg-ink-3"
            >
              <Download />
              {exportMut.isPending ? "Building bundle…" : "Download backup"}
            </Button>
          </div>
        </Card>

        <Card icon={Upload} title="Restore">
          {importMut.isError && (
            <ErrorAlert>
              <div className="flex flex-col gap-0.5">
                <span className="font-semibold">Restore failed</span>
                <span className="break-words font-mono text-xs">{importMut.error.message}</span>
              </div>
            </ErrorAlert>
          )}

          <input
            ref={fileRef}
            type="file"
            accept=".tar.gz,.tgz,.dump"
            className="hidden"
            onChange={(e) => pick(e.target.files?.[0])}
          />
          {file ? (
            <div className="flex items-center gap-3 rounded-xl border border-line px-4 py-3.5">
              <FileIcon />
              <div className="flex min-w-0 flex-1 flex-col gap-0.5">
                <span title={file.name} className="truncate font-mono text-[13px]">
                  {file.name}
                </span>
                <span className="text-xs text-ink-3">{fileMeta(file)}</span>
              </div>
              <Button
                variant="quiet"
                size="icon"
                aria-label="Remove file"
                disabled={restoring}
                onClick={clearFile}
                className="rounded-lg"
              >
                <X />
              </Button>
            </div>
          ) : (
            <button
              type="button"
              onClick={() => fileRef.current?.click()}
              onDragOver={(e) => e.preventDefault()}
              onDrop={(e) => {
                e.preventDefault();
                pick(e.dataTransfer.files[0]);
              }}
              className="flex min-h-[140px] flex-col items-center justify-center gap-2 rounded-xl border-2 border-dashed border-field bg-row-hover text-ink-2 hover:border-ink-3"
            >
              <Upload className="h-6 w-6 text-ink-3" />
              <span className="font-medium">Choose or drop a file</span>
              <span className="flex gap-1.5">
                {[".tar.gz", ".tgz", ".dump"].map((ext) => (
                  <span key={ext} className="rounded-md bg-soft px-2 py-px font-mono text-xs">
                    {ext}
                  </span>
                ))}
              </span>
            </button>
          )}

          <ol className="flex flex-col gap-2">
            {STEPS.map((text, i) => (
              <li key={text} className="flex items-center gap-2.5 text-[13px] text-ink-2">
                <span
                  aria-hidden="true"
                  className={cn(
                    "flex size-[22px] shrink-0 items-center justify-center rounded-full text-xs font-semibold",
                    steps[i] === "done" && "bg-dot-ok text-white",
                    steps[i] === "doing" && "bg-link text-white",
                    steps[i] === "todo" && "bg-soft text-ink-2",
                  )}
                >
                  {i + 1}
                </span>
                {text}
              </li>
            ))}
          </ol>

          <div className="mt-auto">
            <Button
              variant="danger"
              size="form"
              disabled={!file || restoring}
              onClick={() => setConfirming(true)}
            >
              {restoring ? "Restoring…" : "Restore"}
            </Button>
          </div>
        </Card>
      </div>

      {confirming && file && (
        <FormDialog
          open
          onOpenChange={(open) => !open && setConfirming(false)}
          title="Restore over the live database?"
          meta={`${envName.charAt(0).toUpperCase()}${envName.slice(1)} · tigerduck`}
          width={560}
          footer={
            <>
              <Button variant="line" size="form" onClick={() => setConfirming(false)}>
                Cancel
              </Button>
              <Button
                variant="danger"
                size="form"
                onClick={() => {
                  setConfirming(false);
                  importMut.mutate(file);
                }}
              >
                Restore
              </Button>
            </>
          }
        >
          <div className="flex items-center gap-3 rounded-xl border border-line px-4 py-3.5">
            <FileIcon />
            <div className="flex min-w-0 flex-col gap-0.5">
              <span className="break-all font-mono text-[13px]">{file.name}</span>
              <span className="text-xs text-ink-3">{fileMeta(file)}</span>
            </div>
          </div>
          <div className="flex items-center gap-2 text-[13px] text-ink-2">
            <ShieldCheck className="h-4 w-4 shrink-0 text-ink-3" />
            The live database is dumped first
          </div>
        </FormDialog>
      )}
    </>
  );
}

function Card({
  icon: Icon,
  title,
  children,
}: {
  icon: typeof Download;
  title: string;
  children: React.ReactNode;
}) {
  const id = useId();
  return (
    <section
      aria-labelledby={id}
      className="flex min-w-0 flex-[1_1_420px] flex-col rounded-[14px] border border-line bg-surface"
    >
      <div className="flex items-center gap-2.5 border-b border-line px-6 py-[18px] phone:px-4">
        <Icon className="h-[18px] w-[18px] text-ink-3" />
        <h2 id={id} className="text-[15px] font-semibold">
          {title}
        </h2>
      </div>
      <div className="flex flex-1 flex-col gap-4 px-6 py-5 phone:px-4">{children}</div>
    </section>
  );
}

function Chip({ icon: Icon, children }: { icon: typeof Download; children: React.ReactNode }) {
  return (
    <span className="inline-flex items-center gap-2 rounded-[10px] bg-ground px-3 py-2 font-mono text-xs">
      <Icon className="h-3.5 w-3.5 text-ink-3" />
      {children}
    </span>
  );
}

function FileIcon() {
  return (
    <div
      aria-hidden="true"
      className="flex size-10 shrink-0 items-center justify-center rounded-[10px] bg-badge-info text-badge-info-fg"
    >
      <FileArchive className="h-5 w-5" />
    </div>
  );
}
