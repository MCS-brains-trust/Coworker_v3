import { useQuery } from "@tanstack/react-query";

import { fetchHealth, type Health } from "@/api/health";
import { useCurrentUser } from "@/auth/CurrentUserProvider";

/**
 * Sanity page that ships until Phase 10-3 replaces it with the
 * approval queue. Renders the signed-in user from
 * CurrentUserProvider plus a /health probe so we can see at a
 * glance that the proxy is healthy.
 */
export function HealthPage() {
  const me = useCurrentUser();
  const { data, isPending, isError, error } = useQuery<Health, Error>({
    queryKey: ["health"],
    queryFn: fetchHealth,
  });

  return (
    <main className="mx-auto max-w-2xl px-6 py-12 font-sans">
      <header className="flex items-baseline justify-between">
        <h1 className="text-2xl font-semibold tracking-tight">
          MC &amp; S CoWorker
        </h1>
        {me.status === "authenticated" && (
          <span className="text-sm text-neutral-500">
            {me.user.display_name} · {me.user.firm_slug}
          </span>
        )}
      </header>

      <section className="mt-8 rounded-lg border border-neutral-200 bg-neutral-50 p-6">
        <h2 className="text-sm font-medium uppercase tracking-wider text-neutral-700">
          Backend health
        </h2>
        {isPending && (
          <p className="mt-2 text-sm text-neutral-500">checking…</p>
        )}
        {isError && (
          <p className="mt-2 text-sm text-red-700">
            backend unreachable: {error.message}
          </p>
        )}
        {data && (
          <dl className="mt-2 grid grid-cols-[max-content_1fr] gap-x-4 gap-y-1 text-sm">
            <dt className="text-neutral-500">status</dt>
            <dd className="font-mono">{data.status}</dd>
            <dt className="text-neutral-500">service</dt>
            <dd className="font-mono">{data.service}</dd>
            <dt className="text-neutral-500">version</dt>
            <dd className="font-mono">{data.version}</dd>
            <dt className="text-neutral-500">shadow_mode</dt>
            <dd className="font-mono">{data.shadow_mode}</dd>
          </dl>
        )}
      </section>
    </main>
  );
}
