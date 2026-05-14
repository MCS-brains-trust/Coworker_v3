import { useState, type FormEvent } from "react";

/**
 * Minimal sign-in landing. The principal types their firm slug
 * and the page redirects to ``/auth/microsoft/start/{slug}`` —
 * the backend handles state generation, the consent flow, the
 * callback, and the session cookie. On return the
 * CurrentUserProvider's /me query resolves and RequireAuth lets
 * the principal through.
 *
 * A firm slug input feels clunky vs. a per-firm subdomain or
 * tenant picker, but it works without any extra DNS / discovery
 * machinery for the prototype. Phase 13 (onboarding) will wire a
 * proper landing page.
 */
export function SignInPage() {
  const [slug, setSlug] = useState("");
  const [submitting, setSubmitting] = useState(false);

  function onSubmit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const cleaned = slug.trim().toLowerCase();
    if (!cleaned) {
      return;
    }
    setSubmitting(true);
    // The backend route is a 302 — the browser navigates there
    // and Microsoft takes over for the consent + callback.
    window.location.assign(
      `/auth/microsoft/start/${encodeURIComponent(cleaned)}`,
    );
  }

  return (
    <main className="mx-auto max-w-md px-6 py-16 font-sans">
      <h1 className="text-2xl font-semibold tracking-tight">
        Sign in to CoWorker
      </h1>
      <p className="mt-2 text-sm text-neutral-500">
        Enter your firm's slug to start the Microsoft sign-in.
      </p>

      <form onSubmit={onSubmit} className="mt-8 space-y-4">
        <label className="block">
          <span className="block text-sm font-medium text-neutral-700">
            Firm slug
          </span>
          <input
            type="text"
            value={slug}
            onChange={(e) => setSlug(e.target.value)}
            placeholder="mcands"
            autoFocus
            autoCapitalize="none"
            autoCorrect="off"
            spellCheck={false}
            className="mt-1 block w-full rounded-md border border-neutral-300 px-3 py-2 text-sm shadow-sm focus:border-neutral-500 focus:outline-none focus:ring-1 focus:ring-neutral-500"
          />
        </label>
        <button
          type="submit"
          disabled={!slug.trim() || submitting}
          className="w-full rounded-md bg-neutral-900 px-4 py-2 text-sm font-medium text-white shadow-sm hover:bg-neutral-800 disabled:cursor-not-allowed disabled:bg-neutral-400"
        >
          {submitting ? "redirecting…" : "Continue with Microsoft"}
        </button>
      </form>
    </main>
  );
}
