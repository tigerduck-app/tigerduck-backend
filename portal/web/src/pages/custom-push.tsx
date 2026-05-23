import { Link } from "react-router-dom";
import { Hammer } from "lucide-react";
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card";
import { PageHeader } from "@/components/ui/section";
import { useEnv } from "@/hooks/use-env";

export function CustomPushPage() {
  const env = useEnv();
  const isDev = env.data?.env === "development";

  return (
    <div className="space-y-8">
      <PageHeader
        title="Custom push"
        description="Send a real one-off notification to a specific device (or group) from the operator."
      />

      <Card>
        <CardHeader>
          <div className="flex items-center gap-2">
            <Hammer className="h-5 w-5 text-warning" />
            <CardTitle>Coming soon</CardTitle>
          </div>
          <CardDescription>
            Tracked as a TODO in <code>docs/portal-design.md</code>.
          </CardDescription>
        </CardHeader>
        <CardContent className="text-sm text-muted-foreground">
          {isDev ? (
            <>
              For verifying the APNs transport itself (no DB writes,
              synthetic payloads), use the dev-only{" "}
              <Link
                to="/test"
                className="text-primary underline-offset-4 hover:underline"
              >
                Apple test push
              </Link>{" "}
              page.
            </>
          ) : (
            <>
              No alternate path available in production — broadcast a
              real announcement via{" "}
              <Link
                to="/announcement"
                className="text-primary underline-offset-4 hover:underline"
              >
                Announcement
              </Link>{" "}
              instead.
            </>
          )}
        </CardContent>
      </Card>
    </div>
  );
}
