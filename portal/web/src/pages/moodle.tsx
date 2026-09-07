// Moodle admin pages. The two exported page components are the route
// targets; everything they are built from lives in ./moodle/.

import { PageHeader } from "@/components/ui/section";
import { ActionsTab } from "./moodle/ActionsTab";
import { SyncTab } from "./moodle/SyncTab";

export function MoodlePage() {
  return (
    <>
      <PageHeader
        title="Moodle"
        description="Manage server-side Moodle sync jobs"
      />
      <ActionsTab />
    </>
  );
}

export function DataInspectionPage() {
  return (
    <>
      <PageHeader
        title="Data Inspection"
        description="Inspect per-student sync state, device data, and live logs"
      />
      <SyncTab />
    </>
  );
}
