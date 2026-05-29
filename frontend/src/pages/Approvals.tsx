import { useState, useEffect, useCallback } from 'react'
import {
  CheckCircle, XCircle, Edit3, Clock, AlertTriangle,
  ChevronRight, ChevronDown, RefreshCw, Inbox,
} from 'lucide-react'
import { approvals, type Approval, ApiError } from '../api/client'

// ─── Helpers ─────────────────────────────────────────────────────────────────

function field(item: Approval, key: string, fallback = '—'): string {
  const v = (item as Record<string, unknown>)[key]
  return v !== undefined && v !== null ? String(v) : fallback
}

function StatusBadge({ status }: { status: string }) {
  const map: Record<string, { bg: string; color: string }> = {
    pending:  { bg: 'rgba(235,136,31,0.12)', color: '#eb881f' },
    approved: { bg: 'rgba(22,163,74,0.1)',   color: '#16a34a' },
    rejected: { bg: 'rgba(220,38,38,0.1)',   color: '#dc2626' },
  }
  const s = map[status] ?? { bg: '#f3f1ee', color: '#858481' }
  return (
    <span
      className="text-xs font-semibold px-2 py-0.5 uppercase tracking-wider"
      style={{ background: s.bg, color: s.color }}
    >
      {status}
    </span>
  )
}

// ─── Detail pane ─────────────────────────────────────────────────────────────

function DetailPane({
  item,
  onApprove,
  onReject,
}: {
  item: Approval
  onApprove: (id: string, draft?: string) => Promise<void>
  onReject:  (id: string, reason?: string) => Promise<void>
}) {
  const [editMode, setEditMode]         = useState(false)
  const [draft, setDraft]               = useState(field(item, 'draft_body', field(item, 'body', '')))
  const [traceOpen, setTraceOpen]       = useState(false)
  const [busy, setBusy]                 = useState(false)
  const [rejectReason, setRejectReason] = useState('')
  const [showReject, setShowReject]     = useState(false)

  useEffect(() => {
    setDraft(field(item, 'draft_body', field(item, 'body', '')))
    setEditMode(false)
    setShowReject(false)
    setRejectReason('')
  }, [item.id])

  async function handleApprove() {
    setBusy(true)
    try { await onApprove(item.id, editMode ? draft : undefined) }
    finally { setBusy(false) }
  }

  async function handleReject() {
    setBusy(true)
    try { await onReject(item.id, rejectReason || undefined) }
    finally { setBusy(false) }
  }

  const reasoning = Array.isArray((item as Record<string, unknown>).reasoning_steps)
    ? (item as Record<string, unknown>).reasoning_steps as { step: string; duration?: string }[]
    : []

  return (
    <div className="flex-1 overflow-y-auto p-6" style={{ background: '#f3f1ee' }}>
      <div className="max-w-2xl">
        {/* Header card */}
        <div className="bg-white border p-5 mb-4" style={{ borderColor: '#d9d8d8' }}>
          <div className="flex items-center gap-3 mb-3 flex-wrap">
            <StatusBadge status={field(item, 'status')} />
            {field(item, 'confidence') !== '—' && (
              <span className="text-xs font-mono" style={{ color: '#858481' }}>
                conf {parseFloat(field(item, 'confidence')).toFixed(2)}
              </span>
            )}
            <span className="text-xs font-mono" style={{ color: '#858481' }}>
              <Clock size={10} className="inline mr-0.5" />
              {field(item, 'created_at', field(item, 'queued_at', '—'))}
            </span>
          </div>
          <h2
            className="text-xl mb-1"
            style={{ color: '#142234', fontFamily: 'DM Serif Display, serif' }}
          >
            {field(item, 'plugin_name', field(item, 'title', 'Approval'))}
            {field(item, 'client_name', field(item, 'client', '')) !== '—'
              ? ` — ${field(item, 'client_name', field(item, 'client', ''))}`
              : ''}
          </h2>
          {field(item, 'subject') !== '—' && (
            <p className="text-sm" style={{ color: '#858481' }}>
              In response to: &ldquo;{field(item, 'subject')}&rdquo;
            </p>
          )}
        </div>

        {/* Why queued */}
        {field(item, 'why_queued') !== '—' && (
          <div className="bg-white border p-4 mb-4" style={{ borderColor: '#d9d8d8', borderLeft: '4px solid #eb881f' }}>
            <div className="flex items-center gap-2 mb-2">
              <AlertTriangle size={13} style={{ color: '#eb881f' }} />
              <span className="text-xs font-bold uppercase tracking-wider" style={{ color: '#eb881f' }}>
                Why this needs approval
              </span>
            </div>
            <p className="text-sm" style={{ color: '#34322d' }}>{field(item, 'why_queued')}</p>
          </div>
        )}

        {/* Draft */}
        {draft && draft !== '—' && (
          <div className="bg-white border p-5 mb-4" style={{ borderColor: '#d9d8d8' }}>
            <div className="flex items-center justify-between mb-3">
              <span className="text-xs font-bold uppercase tracking-wider" style={{ color: '#858481' }}>
                Proposed draft
              </span>
              {item.status === 'pending' && (
                <button
                  className="flex items-center gap-1 text-xs"
                  style={{ color: '#3080bc' }}
                  onClick={() => setEditMode(!editMode)}
                >
                  <Edit3 size={12} />{editMode ? 'Cancel edit' : 'Edit'}
                </button>
              )}
            </div>
            {editMode ? (
              <textarea
                className="w-full text-sm p-3 border font-mono resize-none focus:outline-none"
                style={{ borderColor: '#3080bc', color: '#34322d', minHeight: '180px' }}
                value={draft}
                onChange={e => setDraft(e.target.value)}
              />
            ) : (
              <pre className="text-sm whitespace-pre-wrap font-sans" style={{ color: '#34322d' }}>{draft}</pre>
            )}
          </div>
        )}

        {/* Reasoning trace */}
        {reasoning.length > 0 && (
          <div className="bg-white border mb-4" style={{ borderColor: '#d9d8d8' }}>
            <button
              className="w-full flex items-center justify-between px-5 py-3 text-left"
              onClick={() => setTraceOpen(!traceOpen)}
            >
              <span className="text-xs font-bold uppercase tracking-wider" style={{ color: '#858481' }}>
                Reasoning trace
              </span>
              {traceOpen
                ? <ChevronDown size={14} style={{ color: '#858481' }} />
                : <ChevronRight size={14} style={{ color: '#858481' }} />}
            </button>
            {traceOpen && (
              <div className="px-5 pb-4 border-t" style={{ borderColor: '#f3f1ee' }}>
                {reasoning.map((r, i) => (
                  <div key={i} className="flex items-start gap-3 py-2 border-b last:border-0" style={{ borderColor: '#f3f1ee' }}>
                    <span className="text-xs font-mono w-5 flex-shrink-0 mt-0.5" style={{ color: '#858481' }}>{i + 1}</span>
                    <span className="text-sm flex-1" style={{ color: '#34322d' }}>▸ {r.step}</span>
                    {r.duration && (
                      <span className="text-xs font-mono flex-shrink-0" style={{ color: '#858481' }}>{r.duration}</span>
                    )}
                  </div>
                ))}
              </div>
            )}
          </div>
        )}

        {/* Actions — pending only */}
        {item.status === 'pending' && (
          <>
            {showReject && (
              <div className="bg-white border p-4 mb-4" style={{ borderColor: '#d9d8d8' }}>
                <label className="text-xs font-bold uppercase tracking-wider block mb-2" style={{ color: '#858481' }}>
                  Rejection reason (optional)
                </label>
                <textarea
                  className="w-full text-sm p-3 border resize-none focus:outline-none"
                  style={{ borderColor: '#d9d8d8', color: '#34322d', minHeight: '80px' }}
                  placeholder="Why are you rejecting this?"
                  value={rejectReason}
                  onChange={e => setRejectReason(e.target.value)}
                />
              </div>
            )}
            <div className="flex gap-3 flex-wrap">
              {!showReject ? (
                <button
                  className="btn-danger flex items-center gap-2"
                  onClick={() => setShowReject(true)}
                  disabled={busy}
                >
                  <XCircle size={14} /> Reject
                </button>
              ) : (
                <>
                  <button
                    className="btn-danger flex items-center gap-2"
                    onClick={handleReject}
                    disabled={busy}
                  >
                    <XCircle size={14} /> Confirm reject
                  </button>
                  <button
                    className="btn-secondary flex items-center gap-2"
                    onClick={() => setShowReject(false)}
                    disabled={busy}
                  >
                    Cancel
                  </button>
                </>
              )}
              {!showReject && (
                <>
                  <button
                    className="btn-secondary flex items-center gap-2"
                    onClick={() => setEditMode(true)}
                    disabled={busy}
                  >
                    <Edit3 size={14} /> Edit &amp; Approve
                  </button>
                  <button
                    className="btn-primary flex items-center gap-2 ml-auto"
                    onClick={handleApprove}
                    disabled={busy}
                  >
                    <CheckCircle size={14} /> {busy ? 'Saving…' : 'Approve as-is'}
                  </button>
                </>
              )}
            </div>
          </>
        )}
      </div>
    </div>
  )
}

// ─── Main page ────────────────────────────────────────────────────────────────

export default function ApprovalsPage() {
  const [items, setItems]       = useState<Approval[]>([])
  const [total, setTotal]       = useState(0)
  const [loading, setLoading]   = useState(true)
  const [error, setError]       = useState<string | null>(null)
  const [selected, setSelected] = useState<Approval | null>(null)
  const [statusFilter, setStatusFilter] = useState<'pending' | 'approved' | 'rejected'>('pending')

  const load = useCallback(async () => {
    setLoading(true)
    setError(null)
    try {
      const data = await approvals.list({ status: statusFilter, limit: 50 })
      setItems(data.items)
      setTotal(data.total)
      setSelected(prev => {
        if (!prev) return data.items[0] ?? null
        const refreshed = data.items.find(i => i.id === prev.id)
        return refreshed ?? data.items[0] ?? null
      })
    } catch (err) {
      if (err instanceof ApiError) {
        setError(`${err.code}: ${err.message}`)
      } else {
        setError('Failed to load approvals')
      }
    } finally {
      setLoading(false)
    }
  }, [statusFilter])

  useEffect(() => {
    setSelected(null)
    load()
  }, [statusFilter])

  async function handleApprove(id: string, editedDraft?: string) {
    await approvals.approve(id, editedDraft)
    load()
  }

  async function handleReject(id: string, reason?: string) {
    await approvals.reject(id, reason)
    load()
  }

  return (
    <div className="flex h-full overflow-hidden">
      {/* Left pane */}
      <div
        className="w-80 flex-shrink-0 flex flex-col border-r overflow-hidden"
        style={{ background: 'white', borderColor: '#d9d8d8' }}
      >
        {/* Toolbar */}
        <div className="px-4 py-3 border-b flex items-center justify-between" style={{ borderColor: '#d9d8d8' }}>
          <div className="flex gap-1">
            {(['pending', 'approved', 'rejected'] as const).map(s => (
              <button
                key={s}
                onClick={() => setStatusFilter(s)}
                className="text-xs px-2 py-1 capitalize font-medium"
                style={{
                  background: statusFilter === s ? '#142234' : 'transparent',
                  color: statusFilter === s ? 'white' : '#858481',
                }}
              >
                {s}
              </button>
            ))}
          </div>
          <button
            onClick={load}
            className="text-xs flex items-center gap-1"
            style={{ color: '#3080bc' }}
            title="Refresh"
          >
            <RefreshCw size={12} className={loading ? 'animate-spin' : ''} />
          </button>
        </div>

        {/* List */}
        <div className="flex-1 overflow-y-auto">
          {loading && (
            <div className="flex items-center justify-center h-32 text-sm" style={{ color: '#858481' }}>
              Loading…
            </div>
          )}
          {!loading && error && (
            <div className="p-4 text-sm" style={{ color: '#dc2626' }}>{error}</div>
          )}
          {!loading && !error && items.length === 0 && (
            <div className="flex flex-col items-center justify-center h-48 gap-3">
              <Inbox size={32} style={{ color: '#d9d8d8' }} />
              <p className="text-sm" style={{ color: '#858481' }}>No {statusFilter} approvals</p>
            </div>
          )}
          {!loading && !error && items.map(item => (
            <div
              key={item.id}
              onClick={() => setSelected(item)}
              className="px-4 py-3 border-b cursor-pointer"
              style={{
                borderColor: '#f3f1ee',
                background: selected?.id === item.id ? '#f3f1ee' : 'white',
                borderLeft: selected?.id === item.id ? '3px solid #eb881f' : '3px solid transparent',
              }}
            >
              <div className="flex items-center justify-between mb-1">
                <span className="text-xs font-semibold truncate" style={{ color: '#142234' }}>
                  {field(item, 'plugin_name', field(item, 'title', 'Approval'))}
                </span>
                <StatusBadge status={field(item, 'status')} />
              </div>
              <div className="text-xs truncate mb-1" style={{ color: '#34322d' }}>
                {field(item, 'client_name', field(item, 'client', item.id))}
              </div>
              <div className="text-xs" style={{ color: '#858481' }}>
                {field(item, 'created_at', field(item, 'queued_at', ''))}
              </div>
            </div>
          ))}
        </div>

        {/* Footer */}
        <div className="px-4 py-2 border-t text-xs" style={{ borderColor: '#d9d8d8', color: '#858481' }}>
          {total} total · showing {items.length}
        </div>
      </div>

      {/* Right pane */}
      {selected ? (
        <DetailPane
          item={selected}
          onApprove={handleApprove}
          onReject={handleReject}
        />
      ) : (
        <div className="flex-1 flex items-center justify-center" style={{ color: '#858481' }}>
          <div className="text-center">
            <CheckCircle size={40} style={{ color: '#d9d8d8', margin: '0 auto 12px' }} />
            <p className="text-sm">Select an approval to review</p>
          </div>
        </div>
      )}
    </div>
  )
}
