import { useRef, useState } from "react";
import { useMutation } from "@tanstack/react-query";
import { toast } from "sonner";
import { CheckCircle2, Download, FileUp, Upload } from "lucide-react";
import { Button } from "@/components/ui/button";
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { PageHeader, Section } from "@/components/ui/section";
import { ApiError } from "@/lib/api";

export function BackupPage() {
  const [imported, setImported] = useState<string | null>(null);
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
    mutationFn: async (file: File) => {
      const fd = new FormData();
      fd.append("file", file);
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
      setImported(body.imported_at);
      toast.success("Import complete");
      if (fileRef.current) fileRef.current.value = "";
    },
    onError: (e) => toast.error((e as Error).message),
  });

  return (
    <div className="space-y-8">
      <PageHeader
        title="Backup & restore"
        description="Database-level export/import of the tigerduck postgres database."
      />

      {imported && (
        <Card className="border-success/40 bg-success/5">
          <CardContent className="flex items-start gap-3">
            <CheckCircle2 className="mt-0.5 h-5 w-5 shrink-0 text-success" />
            <div className="text-sm">
              <div className="font-medium">Import complete.</div>
              <div className="mt-1 text-muted-foreground">
                Restart the backend container manually so FastAPI re-reads
                state:
              </div>
              <pre className="mt-2 rounded bg-muted px-3 py-2 font-mono text-xs">
                ./stop.sh &amp;&amp; ./start.sh
              </pre>
            </div>
          </CardContent>
        </Card>
      )}

      <Section>
        <Card>
          <CardHeader>
            <div className="flex items-center gap-2">
              <Download className="h-5 w-5 text-muted-foreground" />
              <CardTitle>Export</CardTitle>
            </div>
            <CardDescription>
              Bundles <code>pg_dump --format=custom</code> of the tigerduck
              database plus a manifest into{" "}
              <code>tigerduck-export-&lt;timestamp&gt;.tar.gz</code>. No
              secrets are included (APNs .p8, FCM JSON, .env stay on the
              source machine).
            </CardDescription>
          </CardHeader>
          <CardContent>
            <Button
              onClick={() => exportMut.mutate()}
              disabled={exportMut.isPending}
            >
              {exportMut.isPending ? "Building bundle…" : "Download backup"}
            </Button>
          </CardContent>
        </Card>
      </Section>

      <Section>
        <Card>
          <CardHeader>
            <div className="flex items-center gap-2">
              <Upload className="h-5 w-5 text-muted-foreground" />
              <CardTitle>Import</CardTitle>
            </div>
            <CardDescription>
              Accepts either a <code>.tar.gz</code> produced by Export above,
              or a bare <code>pg_dump</code> file from a pre-portal install.
              The portal does NOT restart the backend for you.
            </CardDescription>
          </CardHeader>
          <CardContent className="space-y-3">
            <Input
              ref={fileRef}
              type="file"
              accept=".tar.gz,.tgz,.dump"
              disabled={importMut.isPending}
            />
            <Button
              variant="destructive"
              disabled={importMut.isPending}
              onClick={() => {
                const f = fileRef.current?.files?.[0];
                if (!f) {
                  toast.error("Pick a file first.");
                  return;
                }
                importMut.mutate(f);
              }}
            >
              <FileUp className="mr-1 h-4 w-4" />
              {importMut.isPending ? "Restoring…" : "Restore"}
            </Button>
          </CardContent>
        </Card>
      </Section>
    </div>
  );
}
