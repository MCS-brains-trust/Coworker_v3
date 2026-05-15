import { useQuery } from "@tanstack/react-query";
import { Link } from "react-router-dom";

import { fetchPendingApprovals, type ApprovalItem } from "@/api/approval";
import { useCurrentUser } from "@/auth/CurrentUserProvider";
import { formatRelative } from "@/lib/time";

/**
 * The principal's inbox. Renders every pending item for the
 * signed-in firm; row click navigates to /approval/{id} (the
 * Phase 10-4 detail page).
 *
 * Auto-approved items don't appear here — they're already
 * ``approved`` and skip the queue entirely. Two-person items
 * stay until both signatures land; the row shows "1 of 2" so
 * the second reviewer knows there's a pending cosignature.
 */
export function ApprovalQueuePage() {
  const me = useCurrentUser();
  const { data, isPending, isError, error, refetch, isFetching } =
    useQuery<ApprovalItem[], Error>({
      queryKey: ["approval", "pending"],
      queryFn: fetchPendingApprovals,
    });

  return (
    <main className="mx-auto max-w-3xl px-4 py-6 font-sans sm:px-6 sm:py-10">
      <header className="flex flex-col gap-3 sm:flex-row sm:items-baseline sm:justify-between">
        <div>
          <h1 className="text-xl font-semibold tracking-tight sm:text-2xl">
            Pending review
          </h1>
          <p className="mt-1 text-sm text-neutral-500">
            Approvals waiting on your sign-off.
          </p>
        </div>
        <div className="flex flex-wrap items-center gap-2 text-sm sm:gap-3">
          {me.status === "authenticated" && (
            <span className="truncate text-neutral-500">
              {me.user.display_name} · {me.user.firm_slug}
            </span>
          )}
          <button
            type="button"
            onClick={() => refetch()}
            disabled={isFetching}
            className="ml-auto rounded-md border border-neutral-300 px-2 py-1 text-xs hover:bg-neutral-50 disabled:opacity-50 sm:ml-0"
          >
            {isFetching ? "refreshing…" : "refresh"}
          </button>
        </div>
      </header>

      <section className="mt-6 sm:mt-8">
        {isPending && (
          <p className="text-sm text-neutral-500">loading…</p>
        )}
        {isError && (
          <p className="text-sm text-red-700">{error.message}</p>
        )}
        {data && data.length === 0 && (
          <div className="rounded-lg border border-dashed border-neutral-300 p-8 text-center text-sm text-neutral-500 sm:p-10">
            Inbox zero — nothing pending right now.
          </div>
        )}
        {data && data.length > 0 && (
          <ul className="divide-y divide-neutral-200 rounded-lg border border-neutral-200 bg-white">
            {data.map((item) => (
              <ApprovalRow key={item.id} item={item} />
            ))}
          </ul>
        )}
      </section>
    </main>
  );
}

function ApprovalRow({ item }: { item: ApprovalItem }) {
  const cosignersNeeded = item.required_approvals > 1;
  const sigCount = item.approval_signatures.length;
  return (
    <li>
      <Link
        to={`/approval/${item.id}`}
        className="flex flex-col gap-2 px-4 py-3 hover:bg-neutral-50"
      >
        <div className="flex items-baseline justify-between gap-3">
          <span className="truncate text-sm font-medium text-neutral-900">
            {item.summary}
          </span>
          <span className="shrink-0 text-xs text-neutral-500">
            {formatRelative(item.created_at)}
          </span>
        </div>
        <div className="flex flex-wrap items-center gap-2 text-xs text-neutral-600">
          <Tag>{item.category}</Tag>
          <Tag>{item.plugin_name}</Tag>
          {item.confidence !== null && (
            <Tag>
              confidence {(item.confidence * 100).toFixed(0)}%
            </Tag>
          )}
          {cosignersNeeded && (
            <Tag tone="warn">
              {sigCount} of {item.required_approvals} signatures
            </Tag>
          )}
        </div>
      </Link>
    </li>
  );
}

function Tag({
  children,
  tone = "default",
}: {
  children: React.ReactNode;
  tone?: "default" | "warn";
}) {
  const palette =
    tone === "warn"
      ? "bg-amber-100 text-amber-900"
      : "bg-neutral-100 text-neutral-700";
  return (
    <span
      className={`inline-flex items-center rounded-full px-2 py-0.5 text-xs ${palette}`}
    >
      {children}
    </span>
  );
}
