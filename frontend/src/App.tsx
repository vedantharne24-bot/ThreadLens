import { MotionConfig } from "motion/react";
import { Suspense, lazy, useEffect } from "react";
import { Navigate, Route, Routes } from "react-router-dom";

import { AppLayout } from "@/components/Layout";
import { Skeleton } from "@/components/ui";
import { useReducedEffects } from "@/hooks/usePrefs";
import { restoreActiveSession } from "@/services/session";

const Landing = lazy(() => import("@/pages/Landing"));
const Overview = lazy(() => import("@/pages/Overview"));
const Threads = lazy(() => import("@/pages/Threads"));
const GraphPage = lazy(() => import("@/pages/GraphPage"));
const Events = lazy(() => import("@/pages/Events"));
const Diagnostics = lazy(() => import("@/pages/Diagnostics"));
const Replay = lazy(() => import("@/pages/Replay"));
const TestLab = lazy(() => import("@/pages/TestLab"));
const SettingsPage = lazy(() => import("@/pages/Settings"));

function PageFallback() {
  return (
    <div className="space-y-4">
      <Skeleton className="h-8 w-64" />
      <div className="grid grid-cols-2 gap-4 lg:grid-cols-4">
        {Array.from({ length: 4 }, (_, i) => (
          <Skeleton key={i} className="h-24" />
        ))}
      </div>
      <Skeleton className="h-80" />
    </div>
  );
}

export function App() {
  const reduced = useReducedEffects();
  useEffect(() => restoreActiveSession(), []);
  return (
    <MotionConfig reducedMotion={reduced ? "always" : "never"}>
      <Routes>
        <Route
          path="/"
          element={
            <Suspense fallback={<div className="h-full bg-bg" />}>
              <Landing />
            </Suspense>
          }
        />
        <Route
          path="/app/*"
          element={
            <AppLayout>
              <Suspense fallback={<PageFallback />}>
                <Routes>
                  <Route index element={<Overview />} />
                  <Route path="threads" element={<Threads />} />
                  <Route path="graph" element={<GraphPage />} />
                  <Route path="events" element={<Events />} />
                  <Route path="diagnostics" element={<Diagnostics />} />
                  <Route path="replay" element={<Replay />} />
                  <Route path="lab" element={<TestLab />} />
                  <Route path="settings" element={<SettingsPage />} />
                  <Route path="*" element={<Navigate to="/app" replace />} />
                </Routes>
              </Suspense>
            </AppLayout>
          }
        />
        <Route path="*" element={<Navigate to="/" replace />} />
      </Routes>
    </MotionConfig>
  );
}
