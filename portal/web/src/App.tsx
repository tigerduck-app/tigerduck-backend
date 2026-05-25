import { Navigate, Route, Routes } from "react-router-dom";
import { Layout } from "@/components/layout";
import { StatusPage } from "@/pages/status";
import { LogsPage } from "@/pages/logs";
import { BackupPage } from "@/pages/backup";
import { AnnouncementPage } from "@/pages/announcement";
import { CustomPushPage } from "@/pages/custom-push";
import { DevicesPage } from "@/pages/devices";
import { AppleTestPushPage } from "@/pages/apple-test-push";

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
        <Route path="test" element={<AppleTestPushPage />} />
        <Route path="*" element={<Navigate to="/" replace />} />
      </Route>
    </Routes>
  );
}
