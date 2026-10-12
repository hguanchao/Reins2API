import { lazy, Suspense } from "react";
import { Navigate, Route, Routes } from "react-router-dom";
import { AppShell } from "@/components/layout";

const OpenCodePage = lazy(() =>
  import("@/pages/OpenCodePage").then((module) => ({ default: module.OpenCodePage })),
);

const WorkBuddyPage = lazy(() =>
  import("@/pages/WorkBuddyPage").then((module) => ({ default: module.WorkBuddyPage })),
);

const CodexPage = lazy(() =>
  import("@/pages/CodexPage").then((module) => ({ default: module.CodexPage })),
);

const SettingsPage = lazy(() =>
  import("@/pages/SettingsPage").then((module) => ({ default: module.SettingsPage })),
);

export default function App() {
  return (
    <Suspense fallback={null}>
      <Routes>
        <Route element={<AppShell />}>
          {/* 首页有自己的一节地址：/ 只做重定向（replace，不占历史）。
              这样 OpenCode 页在地址栏里可辨识、可直连、可收藏，刷新也不会掉回来 */}
          <Route index element={<Navigate to="/opencode" replace />} />
          <Route path="opencode" element={<OpenCodePage />} />
          <Route path="workbuddy" element={<WorkBuddyPage />} />
          <Route path="codex" element={<CodexPage />} />
          <Route path="settings" element={<SettingsPage />} />
          <Route path="*" element={<Navigate to="/opencode" replace />} />
        </Route>
      </Routes>
    </Suspense>
  );
}
