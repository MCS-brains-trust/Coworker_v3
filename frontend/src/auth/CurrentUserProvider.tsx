import { createContext, useContext, type ReactNode } from "react";
import { useQuery } from "@tanstack/react-query";

import {
  fetchCurrentUser,
  UnauthenticatedError,
  type CurrentUser,
} from "@/api/me";

type CurrentUserState =
  | { status: "loading" }
  | { status: "anonymous" }
  | { status: "error"; error: Error }
  | { status: "authenticated"; user: CurrentUser };

const CurrentUserContext = createContext<CurrentUserState | undefined>(
  undefined,
);

/**
 * Wraps the app in a long-lived ``/auth/me`` query. The whole
 * tree can read the current user via ``useCurrentUser`` without
 * re-fetching, and the RequireAuth route guard reads the same
 * cached state.
 */
export function CurrentUserProvider({ children }: { children: ReactNode }) {
  const query = useQuery<CurrentUser, Error>({
    queryKey: ["currentUser"],
    queryFn: fetchCurrentUser,
    // Refetch on focus so a stale session expiring while the
    // tab was backgrounded surfaces immediately.
    refetchOnWindowFocus: true,
    // The /me check is cheap; staleTime keeps it from refetching
    // every render but still lets focus / explicit invalidations
    // re-check.
    staleTime: 30_000,
    // 401 is a normal outcome (signed-out) — don't retry it.
    retry: (failureCount, error) =>
      !(error instanceof UnauthenticatedError) && failureCount < 1,
  });

  let state: CurrentUserState;
  if (query.isPending) {
    state = { status: "loading" };
  } else if (query.isError) {
    state =
      query.error instanceof UnauthenticatedError
        ? { status: "anonymous" }
        : { status: "error", error: query.error };
  } else {
    state = { status: "authenticated", user: query.data };
  }

  return (
    <CurrentUserContext.Provider value={state}>
      {children}
    </CurrentUserContext.Provider>
  );
}

export function useCurrentUser(): CurrentUserState {
  const ctx = useContext(CurrentUserContext);
  if (!ctx) {
    throw new Error(
      "useCurrentUser must be called inside a CurrentUserProvider",
    );
  }
  return ctx;
}
