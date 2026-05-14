import { Navigate, useLocation } from "react-router-dom";
import type { ReactNode } from "react";

import { useCurrentUser } from "@/auth/CurrentUserProvider";

/**
 * Route guard. Renders ``children`` when the current user is
 * resolved; redirects to /signin otherwise. The original path is
 * preserved in router state so the post-login flow can land back
 * there (handled in Phase 10-3 once the approval routes exist).
 */
export function RequireAuth({ children }: { children: ReactNode }) {
  const state = useCurrentUser();
  const location = useLocation();

  if (state.status === "loading") {
    return (
      <main className="mx-auto max-w-2xl px-6 py-12 text-sm text-neutral-500">
        checking session…
      </main>
    );
  }

  if (state.status === "error") {
    return (
      <main className="mx-auto max-w-2xl px-6 py-12">
        <h1 className="text-lg font-semibold">Couldn't reach the backend</h1>
        <p className="mt-2 text-sm text-neutral-600">{state.error.message}</p>
      </main>
    );
  }

  if (state.status === "anonymous") {
    return (
      <Navigate
        to="/signin"
        replace
        state={{ from: location.pathname + location.search }}
      />
    );
  }

  return <>{children}</>;
}
