import { Navigate, Route, Routes } from "react-router-dom";
import { Layout } from "@/components/layout";
import { StatusPage } from "@/pages/status";
import { LogsPage } from "@/pages/logs";
import { BackupPage } from "@/pages/backup";
import { AnnouncementPage } from "@/pages/announcement";
import { CustomPushPage } from "@/pages/custom-push";
import { DevicesPage } from "@/pages/devices";
import { ListsPage, ListDetailPage } from "@/pages/lists";
import { MoodlePage, DataInspectionPage } from "@/pages/moodle";
import { SemesterDatesPage } from "@/pages/semester-dates";
import { CourseChangesPage } from "@/pages/course-changes";
import { DeregisterPage } from "@/pages/deregister";
import { LinksPage } from "@/pages/links";
import { TlsPinsPage } from "@/pages/tls-pins";
import { StackPage } from "@/pages/stack";

export function App() {
  return (
    <Routes>
      <Route element={<Layout />}>
        <Route index element={<StatusPage />} />
        <Route path="logs" element={<LogsPage />} />
        <Route path="backup" element={<BackupPage />} />
        <Route path="announcement" element={<AnnouncementPage />} />
        <Route path="custom-push" element={<CustomPushPage />} />
        <Route path="devices" element={<DevicesPage />} />
        <Route path="lists" element={<ListsPage />} />
        <Route path="lists/:id" element={<ListDetailPage />} />
        <Route path="moodle" element={<MoodlePage />} />
        <Route path="inspect" element={<DataInspectionPage />} />
        <Route path="semester-dates" element={<SemesterDatesPage />} />
        <Route path="course-changes" element={<CourseChangesPage />} />
        <Route path="links" element={<LinksPage />} />
        <Route path="deregister" element={<DeregisterPage />} />
        <Route path="tls-pins" element={<TlsPinsPage />} />
        <Route path="stack" element={<StackPage />} />
        <Route path="*" element={<Navigate to="/" replace />} />
      </Route>
    </Routes>
  );
}
