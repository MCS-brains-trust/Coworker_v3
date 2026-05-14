import { Navigate, Route, Routes } from "react-router-dom";

import { CurrentUserProvider } from "@/auth/CurrentUserProvider";
import { RequireAuth } from "@/auth/RequireAuth";
import { ApprovalQueuePage } from "@/pages/ApprovalQueuePage";
import { HealthPage } from "@/pages/HealthPage";
import { SignInPage } from "@/pages/SignInPage";

/**
 * Top-level router.
 *
 * - ``/signin`` is the only un-authed route; everything else
 *   sits behind ``RequireAuth``.
 * - ``/`` redirects to ``/approval`` — the principal's daily
 *   landing.
 * - ``/health`` is kept for ops/debug sanity (Phase 10-1).
 * - ``/approval`` lists pending items (Phase 10-3); the
 *   detail page at ``/approval/:id`` lands in Phase 10-4.
 */
export function App() {
  return (
    <CurrentUserProvider>
      <Routes>
        <Route path="/signin" element={<SignInPage />} />
        <Route
          path="/"
          element={<Navigate to="/approval" replace />}
        />
        <Route
          path="/approval"
          element={
            <RequireAuth>
              <ApprovalQueuePage />
            </RequireAuth>
          }
        />
        <Route
          path="/health"
          element={
            <RequireAuth>
              <HealthPage />
            </RequireAuth>
          }
        />
        <Route
          path="*"
          element={<Navigate to="/approval" replace />}
        />
      </Routes>
    </CurrentUserProvider>
  );
}
