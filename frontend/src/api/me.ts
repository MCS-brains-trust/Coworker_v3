/**
 * Backend /auth/me shape. The CurrentUserProvider hits this on
 * mount; 401 means "not signed in" and the UI routes to /signin.
 * Any other error is surfaced as a banner.
 */
export type CurrentUser = {
  user_id: string;
  firm_id: string;
  firm_slug: string;
  upn: string;
  display_name: string;
  role: string;
};

export class UnauthenticatedError extends Error {
  constructor() {
    super("not authenticated");
    this.name = "UnauthenticatedError";
  }
}

export async function fetchCurrentUser(): Promise<CurrentUser> {
  const response = await fetch("/auth/me", { credentials: "include" });
  if (response.status === 401) {
    throw new UnauthenticatedError();
  }
  if (!response.ok) {
    throw new Error(`/auth/me returned HTTP ${response.status}`);
  }
  return (await response.json()) as CurrentUser;
}
