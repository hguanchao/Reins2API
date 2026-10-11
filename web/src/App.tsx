import { lazy, Suspense } from "react";
import { Navigate, Route, Routes } from "react-router-dom";
import { AppShell } from "@/components/layout";

const OpenCodePage = lazy(() =>
  import("@/pages/OpenCodePage").then((module) => ({ default: module.OpenCodePage })),
);

const WorkBuddyPage = lazy(() =>
  import("@/pages/WorkBuddyPage").then((module) => ({ default: module.WorkBuddyPage })),
);

const SettingsPage = lazy(() =>
  import("@/pages/SettingsPage").then((module) => ({ default: module.SettingsPage })),
);

export default function App() {
  return (
    <Suspense fallback={null}>
      <Routes>
        <Route element={<AppShell />}>
          <Route index element={<OpenCodePage />} />
          <Route path="workbuddy" element={<WorkBuddyPage />} />
          <Route path="settings" element={<SettingsPage />} />
          <Route path="*" element={<Navigate to="/" replace />} />
        </Route>
      </Routes>
    </Suspense>
  );
}
