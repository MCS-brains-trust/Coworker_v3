/**
 * Approval queue API surface.
 *
 * Shape matches ``ApprovalItemResponse`` on the backend
 * (coworker.api.routes.approval). Hand-authored because the
 * backend doesn't yet emit an OpenAPI client — Phase 10-3
 * only needs read + decide + edit, all narrow surfaces.
 */
export type ApprovalSignature = {
  user_id: string | null;
  signed_at: string;
  notes: string | null;
};

export type ApprovalItem = {
  id: string;
  trace_id: string | null;
  plugin_name: string;
  category: string;
  summary: string;
  payload: Record<string, unknown>;
  status: "pending" | "approved" | "rejected" | "sent" | "dispatch_failed";
  decided_at: string | null;
  decided_by_user_id: string | null;
  decision_notes: string | null;
  last_edited_at: string | null;
  last_edited_by_user_id: string | null;
  required_approvals: number;
  approval_signatures: ApprovalSignature[];
  confidence: number | null;
  created_at: string;
  updated_at: string;
};

export async function fetchPendingApprovals(): Promise<ApprovalItem[]> {
  const response = await fetch("/approval/pending", {
    credentials: "include",
  });
  if (!response.ok) {
    throw new Error(`/approval/pending returned HTTP ${response.status}`);
  }
  return (await response.json()) as ApprovalItem[];
}

export async function fetchApproval(id: string): Promise<ApprovalItem> {
  const response = await fetch(`/approval/${encodeURIComponent(id)}`, {
    credentials: "include",
  });
  if (!response.ok) {
    throw new Error(
      `/approval/${id} returned HTTP ${response.status}`,
    );
  }
  return (await response.json()) as ApprovalItem;
}

export async function approveItem(
  id: string,
  notes?: string,
): Promise<ApprovalItem> {
  return await _decide(id, "approve", notes);
}

export async function rejectItem(
  id: string,
  notes?: string,
): Promise<ApprovalItem> {
  return await _decide(id, "reject", notes);
}

async function _decide(
  id: string,
  verb: "approve" | "reject",
  notes: string | undefined,
): Promise<ApprovalItem> {
  const response = await fetch(
    `/approval/${encodeURIComponent(id)}/${verb}`,
    {
      method: "POST",
      credentials: "include",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ notes: notes ?? null }),
    },
  );
  if (!response.ok) {
    const body = await response.text();
    throw new Error(
      `/approval/${id}/${verb} returned HTTP ${response.status}: ${body}`,
    );
  }
  return (await response.json()) as ApprovalItem;
}

export async function editPayload(
  id: string,
  payload: Record<string, unknown>,
): Promise<ApprovalItem> {
  const response = await fetch(
    `/approval/${encodeURIComponent(id)}/payload`,
    {
      method: "PUT",
      credentials: "include",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ payload }),
    },
  );
  if (!response.ok) {
    const body = await response.text();
    throw new Error(
      `/approval/${id}/payload returned HTTP ${response.status}: ${body}`,
    );
  }
  return (await response.json()) as ApprovalItem;
}
