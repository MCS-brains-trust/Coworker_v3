import { Route, Routes } from "react-router-dom";

import { CurrentUserProvider } from "@/auth/CurrentUserProvider";
import { RequireAuth } from "@/auth/RequireAuth";
import { HealthPage } from "@/pages/HealthPage";
import { SignInPage } from "@/pages/SignInPage";

/**
 * Top-level router. Phase 10-2 lands the OAuth sign-in flow:
 * unauthenticated visits redirect to /signin which redirects to
 * the backend's /auth/microsoft/start/{slug}. Phase 10-3 wires
 * the approval queue list at /approval.
 */
export function App() {
  return (
    <CurrentUserProvider>
      <Routes>
        <Route path="/signin" element={<SignInPage />} />
        <Route
          path="/"
          element={
            <RequireAuth>
              <HealthPage />
            </RequireAuth>
          }
        />
        <Route
          path="*"
          element={
            <RequireAuth>
              <HealthPage />
            </RequireAuth>
          }
        />
      </Routes>
    </CurrentUserProvider>
  );
}
