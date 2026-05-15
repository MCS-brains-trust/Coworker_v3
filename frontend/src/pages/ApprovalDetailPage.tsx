import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Link, useNavigate, useParams } from "react-router-dom";

import {
  approveItem,
  editPayload,
  fetchApproval,
  rejectItem,
  type ApprovalItem,
  type ApprovalSignature,
} from "@/api/approval";
import { HtmlPreview } from "@/components/HtmlPreview";
import { formatAbsolute, formatRelative } from "@/lib/time";

type BodyView = "preview" | "source";

/**
 * Per-item review surface. Fetches GET /approval/{id}; renders
 * the payload (email_draft gets a tailored view, everything else
 * shows raw JSON); offers Edit / Approve / Reject buttons gated
 * on the item still being ``pending``.
 *
 * Terminal items (approved / rejected / sent / dispatch_failed)
 * render read-only with the decided metadata.
 */
export function ApprovalDetailPage() {
  const { id } = useParams<{ id: string }>();
  const navigate = useNavigate();
  const queryClient = useQueryClient();

  const { data, isPending, isError, error } = useQuery<ApprovalItem, Error>({
    queryKey: ["approval", id],
    queryFn: () => fetchApproval(id as string),
    enabled: !!id,
  });

  const invalidate = () => {
    queryClient.invalidateQueries({ queryKey: ["approval"] });
  };

  const decideMutation = useMutation({
    mutationFn: async (args: {
      verb: "approve" | "reject";
      notes: string | undefined;
    }) => {
      const fn = args.verb === "approve" ? approveItem : rejectItem;
      return await fn(id as string, args.notes);
    },
    onSuccess: () => {
      invalidate();
      navigate("/approval");
    },
  });

  const editMutation = useMutation({
    mutationFn: async (payload: Record<string, unknown>) => {
      return await editPayload(id as string, payload);
    },
    onSuccess: () => invalidate(),
  });

  if (!id) {
    return <PageShell><p>Missing item id.</p></PageShell>;
  }
  if (isPending) {
    return <PageShell><p className="text-sm text-neutral-500">loading…</p></PageShell>;
  }
  if (isError) {
    return (
      <PageShell>
        <p className="text-sm text-red-700">{error.message}</p>
      </PageShell>
    );
  }

  return (
    <PageShell>
      <header>
        <Link
          to="/approval"
          className="text-xs text-neutral-500 hover:underline"
        >
          ← back to queue
        </Link>
        <h1 className="mt-1 text-xl font-semibold tracking-tight">
          {data.summary}
        </h1>
        <p className="mt-1 text-sm text-neutral-500">
          {data.plugin_name} · {data.category} ·{" "}
          {formatRelative(data.created_at)}
        </p>
      </header>

      <StatusBanner item={data} />

      <PayloadSection
        item={data}
        onSave={(payload) => editMutation.mutate(payload)}
        editLoading={editMutation.isPending}
        editError={editMutation.error}
      />

      {data.required_approvals > 1 && (
        <SignaturesSection signatures={data.approval_signatures} required={data.required_approvals} />
      )}

      {data.status === "pending" && (
        <DecideButtons
          onApprove={(notes) => decideMutation.mutate({ verb: "approve", notes })}
          onReject={(notes) => decideMutation.mutate({ verb: "reject", notes })}
          loading={decideMutation.isPending}
          error={decideMutation.error}
        />
      )}
    </PageShell>
  );
}

function PageShell({ children }: { children: React.ReactNode }) {
  return (
    <main className="mx-auto max-w-3xl space-y-5 px-4 py-6 font-sans sm:space-y-6 sm:px-6 sm:py-10">
      {children}
    </main>
  );
}

// ---------------------------------------------------------------------------
// Status banner
// ---------------------------------------------------------------------------

function StatusBanner({ item }: { item: ApprovalItem }) {
  if (item.status === "pending") {
    return null;
  }
  const palette = {
    approved: "bg-emerald-50 border-emerald-200 text-emerald-900",
    sent: "bg-emerald-50 border-emerald-200 text-emerald-900",
    rejected: "bg-neutral-50 border-neutral-200 text-neutral-700",
    dispatch_failed: "bg-amber-50 border-amber-200 text-amber-900",
  }[item.status];
  return (
    <section
      className={`rounded-md border px-4 py-3 text-sm ${palette}`}
    >
      <p>
        <span className="font-medium">Status:</span> {item.status}
        {item.decided_at && (
          <> · {formatAbsolute(item.decided_at)}</>
        )}
      </p>
      {item.decision_notes && (
        <p className="mt-1 text-neutral-600">{item.decision_notes}</p>
      )}
    </section>
  );
}

// ---------------------------------------------------------------------------
// Payload editor
// ---------------------------------------------------------------------------

function PayloadSection({
  item,
  onSave,
  editLoading,
  editError,
}: {
  item: ApprovalItem;
  onSave: (payload: Record<string, unknown>) => void;
  editLoading: boolean;
  editError: Error | null;
}) {
  if (item.category === "email_draft") {
    return (
      <EmailDraftEditor
        payload={item.payload}
        editable={item.status === "pending"}
        onSave={onSave}
        editLoading={editLoading}
        editError={editError}
      />
    );
  }
  return <RawJsonPayload payload={item.payload} />;
}

function EmailDraftEditor({
  payload,
  editable,
  onSave,
  editLoading,
  editError,
}: {
  payload: Record<string, unknown>;
  editable: boolean;
  onSave: (payload: Record<string, unknown>) => void;
  editLoading: boolean;
  editError: Error | null;
}) {
  const [draftBody, setDraftBody] = useState<string | null>(null);
  // Default to the rendered preview — the principal usually wants
  // to see what the recipient will see, not the HTML source.
  const [view, setView] = useState<BodyView>("preview");

  const currentBody = String(payload.body_html ?? "");
  const isEditing = draftBody !== null;
  // The view tab applies to whichever string is "live" — the
  // draft being edited or the persisted body.
  const liveBody = isEditing ? draftBody : currentBody;

  const to = Array.isArray(payload.to)
    ? (payload.to as string[]).join(", ")
    : "";
  const subject = String(payload.subject ?? "");

  return (
    <section className="space-y-3 rounded-lg border border-neutral-200 bg-white p-4">
      <dl className="grid grid-cols-[max-content_1fr] gap-x-3 gap-y-1 text-sm">
        <dt className="text-neutral-500">To</dt>
        <dd className="break-all font-mono">{to}</dd>
        <dt className="text-neutral-500">Subject</dt>
        <dd className="break-words">{subject}</dd>
      </dl>
      <div>
        <div className="flex flex-wrap items-center justify-between gap-2">
          <h3 className="text-sm font-medium text-neutral-700">Body</h3>
          <div className="flex items-center gap-3">
            <BodyViewToggle view={view} onChange={setView} />
            {editable && !isEditing && (
              <button
                type="button"
                onClick={() => setDraftBody(currentBody)}
                className="text-xs text-blue-700 hover:underline"
              >
                edit
              </button>
            )}
          </div>
        </div>
        <div className="mt-1 space-y-2">
          {view === "preview" ? (
            <HtmlPreview html={liveBody} />
          ) : isEditing ? (
            <textarea
              value={draftBody}
              onChange={(e) => setDraftBody(e.target.value)}
              rows={12}
              className="block w-full rounded-md border border-neutral-300 p-2 font-mono text-xs"
            />
          ) : (
            <pre className="max-h-96 overflow-y-auto whitespace-pre-wrap rounded-md bg-neutral-50 p-3 font-mono text-xs">
              {currentBody}
            </pre>
          )}
          {isEditing && (
            <div className="flex items-center gap-2">
              <button
                type="button"
                disabled={editLoading || draftBody === currentBody}
                onClick={() => {
                  onSave({ ...payload, body_html: draftBody });
                  setDraftBody(null);
                }}
                className="rounded-md bg-neutral-900 px-3 py-1 text-xs font-medium text-white disabled:bg-neutral-400"
              >
                {editLoading ? "saving…" : "save"}
              </button>
              <button
                type="button"
                onClick={() => setDraftBody(null)}
                className="rounded-md border border-neutral-300 px-3 py-1 text-xs"
              >
                cancel
              </button>
              {editError && (
                <span className="text-xs text-red-700">
                  {editError.message}
                </span>
              )}
            </div>
          )}
        </div>
      </div>
    </section>
  );
}

function BodyViewToggle({
  view,
  onChange,
}: {
  view: BodyView;
  onChange: (view: BodyView) => void;
}) {
  return (
    <div
      role="tablist"
      aria-label="body view"
      className="inline-flex overflow-hidden rounded-md border border-neutral-300 text-xs"
    >
      {(["preview", "source"] as BodyView[]).map((v) => (
        <button
          key={v}
          type="button"
          role="tab"
          aria-selected={view === v}
          onClick={() => onChange(v)}
          className={
            view === v
              ? "bg-neutral-900 px-2 py-1 text-white"
              : "bg-white px-2 py-1 text-neutral-700 hover:bg-neutral-50"
          }
        >
          {v}
        </button>
      ))}
    </div>
  );
}

function RawJsonPayload({
  payload,
}: {
  payload: Record<string, unknown>;
}) {
  return (
    <section className="rounded-lg border border-neutral-200 bg-white p-4">
      <h3 className="text-sm font-medium text-neutral-700">Payload</h3>
      <pre className="mt-1 max-h-96 overflow-y-auto rounded-md bg-neutral-50 p-3 font-mono text-xs">
        {JSON.stringify(payload, null, 2)}
      </pre>
    </section>
  );
}

// ---------------------------------------------------------------------------
// Signatures (two-person items)
// ---------------------------------------------------------------------------

function SignaturesSection({
  signatures,
  required,
}: {
  signatures: ApprovalSignature[];
  required: number;
}) {
  return (
    <section className="rounded-lg border border-neutral-200 bg-white p-4">
      <h3 className="text-sm font-medium text-neutral-700">
        Signatures ({signatures.length} of {required})
      </h3>
      <ul className="mt-2 space-y-1 text-sm">
        {signatures.map((s, i) => (
          <li key={i} className="flex gap-2 text-neutral-700">
            <span className="text-neutral-500">
              {formatAbsolute(s.signed_at)}
            </span>
            <span className="font-mono text-xs">
              {s.user_id ? s.user_id : "system"}
            </span>
            {s.notes && (
              <span className="text-neutral-600">— {s.notes}</span>
            )}
          </li>
        ))}
        {Array.from({ length: required - signatures.length }).map((_, i) => (
          <li
            key={`pending-${i}`}
            className="text-sm italic text-neutral-400"
          >
            awaiting cosigner…
          </li>
        ))}
      </ul>
    </section>
  );
}

// ---------------------------------------------------------------------------
// Decide buttons
// ---------------------------------------------------------------------------

function DecideButtons({
  onApprove,
  onReject,
  loading,
  error,
}: {
  onApprove: (notes: string | undefined) => void;
  onReject: (notes: string | undefined) => void;
  loading: boolean;
  error: Error | null;
}) {
  const [notes, setNotes] = useState("");
  const noteValue = notes.trim() || undefined;
  return (
    <section className="rounded-lg border border-neutral-200 bg-white p-4">
      <label className="block">
        <span className="block text-sm font-medium text-neutral-700">
          Notes (optional)
        </span>
        <textarea
          value={notes}
          onChange={(e) => setNotes(e.target.value)}
          rows={2}
          className="mt-1 block w-full rounded-md border border-neutral-300 p-2 text-sm"
        />
      </label>
      <div className="mt-3 flex flex-col items-stretch gap-2 sm:flex-row sm:items-center">
        <button
          type="button"
          disabled={loading}
          onClick={() => onApprove(noteValue)}
          className="rounded-md bg-emerald-700 px-4 py-2 text-sm font-medium text-white hover:bg-emerald-800 disabled:bg-neutral-400"
        >
          {loading ? "…" : "approve"}
        </button>
        <button
          type="button"
          disabled={loading}
          onClick={() => onReject(noteValue)}
          className="rounded-md border border-neutral-300 px-4 py-2 text-sm font-medium text-neutral-700 hover:bg-neutral-50 disabled:opacity-50"
        >
          reject
        </button>
        {error && (
          <span className="text-xs text-red-700">{error.message}</span>
        )}
      </div>
    </section>
  );
}
