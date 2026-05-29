/**
 * CoWorker v3 API Client
 *
 * All requests use credentials: 'include' so the HttpOnly session cookie
 * is sent automatically. No Authorization header, no localStorage tokens.
 *
 * Base URL: https://coworker.mcands.com.au
 * API prefix: /api/v1
 */

const BASE = '/api/v1';

export class ApiError {
  name = 'ApiError'
  message: string
  status: number
  code: string
  constructor(status: number, code: string, message: string) {
    this.status = status
    this.code = code
    this.message = message
  }
}

async function request<T>(
  method: string,
  path: string,
  body?: unknown,
): Promise<T> {
  const res = await fetch(path, {
    method,
    credentials: 'include',
    headers: {
      'Content-Type': 'application/json',
      Accept: 'application/json',
    },
    body: body !== undefined ? JSON.stringify(body) : undefined,
  });

  if (!res.ok) {
    let code = 'UNKNOWN';
    let message = `HTTP ${res.status}`;
    try {
      const json = await res.json();
      code = json?.error?.code ?? code;
      message = json?.error?.message ?? message;
    } catch {
      // ignore parse errors
    }
    throw new ApiError(res.status, code, message);
  }

  // 204 No Content
  if (res.status === 204) return undefined as T;
  return res.json() as Promise<T>;
}

// ─── Auth ────────────────────────────────────────────────────────────────────

export interface CurrentUser {
  user_id: string;
  firm_id: string;
  firm_slug: string;
  upn: string;
  display_name: string;
  role: 'owner' | 'principal' | 'accountant' | 'reception' | 'viewer';
}

export const FIRM_SLUG = 'mc-s-accountants';

export const auth = {
  /** Navigate the browser to start the Microsoft OAuth flow */
  startMicrosoftLogin() {
    window.location.href = `${BASE}/auth/microsoft/start/${FIRM_SLUG}`;
  },

  /** Get the currently authenticated user. Throws ApiError(401) if not logged in. */
  me(): Promise<CurrentUser> {
    return request<CurrentUser>('GET', `${BASE}/auth/me`);
  },

  /** Clear the session cookie and log out. */
  logout(): Promise<void> {
    return request<void>('POST', `${BASE}/auth/logout`);
  },
};

// ─── Approvals ───────────────────────────────────────────────────────────────

export type ApprovalStatus = 'pending' | 'approved' | 'rejected';

export interface Approval {
  id: string;
  status: ApprovalStatus;
  [key: string]: unknown; // shape still being finalised — inspect live OpenAPI
}

export interface ApprovalList {
  total: number;
  items: Approval[];
}

export const approvals = {
  list(params?: {
    status?: ApprovalStatus;
    limit?: number;
    offset?: number;
  }): Promise<ApprovalList> {
    const qs = new URLSearchParams();
    if (params?.status) qs.set('status', params.status);
    if (params?.limit !== undefined) qs.set('limit', String(params.limit));
    if (params?.offset !== undefined) qs.set('offset', String(params.offset));
    const query = qs.toString() ? `?${qs}` : '';
    return request<ApprovalList>('GET', `${BASE}/approvals${query}`);
  },

  get(id: string): Promise<Approval> {
    return request<Approval>('GET', `${BASE}/approvals/${id}`);
  },

  approve(id: string, editedDraft?: string): Promise<void> {
    return request<void>('POST', `${BASE}/approvals/${id}/approve`, {
      edited_draft: editedDraft,
    });
  },

  reject(id: string, reason?: string): Promise<void> {
    return request<void>('POST', `${BASE}/approvals/${id}/reject`, {
      reason,
    });
  },

  updatePayload(id: string, payload: unknown): Promise<void> {
    return request<void>('PUT', `${BASE}/approvals/${id}/payload`, payload);
  },
};

// ─── Specialists ─────────────────────────────────────────────────────────────

export interface SpecialistSummary {
  id: string;
  name: string;
  display_name: string;
  description: string;
  is_enabled: boolean;
  model: string;
  extended_thinking: boolean;
  active_version_id: string | null;
  updated_at: string;
}

export interface SpecialistListResponse {
  specialists: SpecialistSummary[];
}

export interface SpecialistPromptResponse {
  id: string;
  name: string;
  display_name: string;
  prompt_text: string;
  version_number: number;
  updated_at: string;
}

export interface SpecialistPromptUpdate {
  prompt_text: string;
  change_summary: string;
}

export const specialists = {
  list(): Promise<SpecialistListResponse> {
    return request<SpecialistListResponse>('GET', `${BASE}/specialists`);
  },

  getPrompt(id: string): Promise<SpecialistPromptResponse> {
    return request<SpecialistPromptResponse>('GET', `${BASE}/specialists/${id}/prompt`);
  },

  updatePrompt(id: string, body: SpecialistPromptUpdate): Promise<SpecialistPromptResponse> {
    return request<SpecialistPromptResponse>('PUT', `${BASE}/specialists/${id}/prompt`, body);
  },
};

// ─── Inbox ───────────────────────────────────────────────────────────────────

export interface InboxItem {
  id: string;
  subject: string;
  from: string;
  received_at: string;
  [key: string]: unknown;
}

export interface InboxList {
  total: number;
  items: InboxItem[];
}

export const inbox = {
  list(): Promise<InboxList> {
    return request<InboxList>('GET', `${BASE}/inbox`);
  },
};
