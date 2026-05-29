import { useEffect, useState } from 'react'
import { CheckCircle, RefreshCw, Users } from 'lucide-react'
import {
  specialists,
  ApiError,
  type SpecialistSummary,
  type SpecialistPromptResponse,
} from '../api/client'
import { useAuth } from '../auth/AuthContext'

const MIN_SUMMARY_LEN = 10

export default function SpecialistsPage() {
  const { user } = useAuth()
  const canEdit = user?.role === 'owner' || user?.role === 'principal'

  const [items, setItems] = useState<SpecialistSummary[]>([])
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)
  const [selectedId, setSelectedId] = useState<string | null>(null)

  const [prompt, setPrompt] = useState<SpecialistPromptResponse | null>(null)
  const [promptLoading, setPromptLoading] = useState(false)
  const [promptError, setPromptError] = useState<string | null>(null)

  const [draftText, setDraftText] = useState('')
  const [changeSummary, setChangeSummary] = useState('')
  const [saving, setSaving] = useState(false)
  const [saveError, setSaveError] = useState<string | null>(null)
  const [savedVersion, setSavedVersion] = useState<number | null>(null)

  async function loadList() {
    setLoading(true)
    setError(null)
    try {
      const data = await specialists.list()
      setItems(data.specialists)
    } catch (err) {
      if (err instanceof ApiError) setError(`${err.code}: ${err.message}`)
      else setError('Failed to load specialists')
    } finally {
      setLoading(false)
    }
  }

  useEffect(() => {
    let cancelled = false
    specialists.list().then(
      (data) => {
        if (cancelled) return
        setItems(data.specialists)
        setLoading(false)
      },
      (err) => {
        if (cancelled) return
        if (err instanceof ApiError) setError(`${err.code}: ${err.message}`)
        else setError('Failed to load specialists')
        setLoading(false)
      },
    )
    return () => {
      cancelled = true
    }
  }, [])

  useEffect(() => {
    if (selectedId === null) {
      return
    }
    let cancelled = false
    // eslint-disable-next-line react-hooks/set-state-in-effect -- pre-fetch UI reset on selection change; the matching final setState calls live inside the .then() handlers below
    setPromptLoading(true)
    setPromptError(null)
    setSaveError(null)
    setSavedVersion(null)
    specialists.getPrompt(selectedId).then(
      (data) => {
        if (cancelled) return
        setPrompt(data)
        setDraftText(data.prompt_text)
        setChangeSummary('')
        setPromptLoading(false)
      },
      (err) => {
        if (cancelled) return
        setPrompt(null)
        if (err instanceof ApiError) setPromptError(`${err.code}: ${err.message}`)
        else setPromptError('Failed to load specialist prompt')
        setPromptLoading(false)
      },
    )
    return () => {
      cancelled = true
    }
  }, [selectedId])

  const textChanged = prompt !== null && draftText !== prompt.prompt_text
  const dirty = textChanged || changeSummary.length > 0
  const summaryValid = changeSummary.trim().length >= MIN_SUMMARY_LEN
  const canSave = canEdit && textChanged && summaryValid && !saving

  async function save() {
    if (!selectedId || !canSave) return
    setSaving(true)
    setSaveError(null)
    setSavedVersion(null)
    try {
      const updated = await specialists.updatePrompt(selectedId, {
        prompt_text: draftText,
        change_summary: changeSummary,
      })
      setPrompt(updated)
      setDraftText(updated.prompt_text)
      setChangeSummary('')
      setSavedVersion(updated.version_number)
      // Refresh the list so updated_at reflects in the left pane on next render
      loadList()
    } catch (err) {
      if (err instanceof ApiError) setSaveError(`${err.code}: ${err.message}`)
      else setSaveError('Failed to save prompt')
    } finally {
      setSaving(false)
    }
  }

  return (
    <div className="flex h-full -m-6 overflow-hidden">
      {/* Left pane */}
      <div
        className="w-80 flex-shrink-0 flex flex-col border-r overflow-hidden"
        style={{ background: 'white', borderColor: '#d9d8d8' }}
      >
        <div
          className="px-4 py-3 border-b flex items-center justify-between"
          style={{ borderColor: '#d9d8d8' }}
        >
          <span
            className="text-xs font-bold uppercase tracking-wider"
            style={{ color: '#858481' }}
          >
            Specialists
          </span>
          <button
            onClick={loadList}
            className="text-xs flex items-center gap-1"
            style={{ color: '#3080bc' }}
            title="Refresh"
            aria-label="Refresh"
          >
            <RefreshCw size={12} className={loading ? 'animate-spin' : ''} />
          </button>
        </div>

        <div className="flex-1 overflow-y-auto">
          {loading && (
            <div
              className="flex items-center justify-center h-32 text-sm"
              style={{ color: '#858481' }}
            >
              Loading…
            </div>
          )}
          {!loading && error && (
            <div className="p-4 text-sm" style={{ color: '#dc2626' }}>
              {error}
            </div>
          )}
          {!loading && !error && items.length === 0 && (
            <div className="flex flex-col items-center justify-center h-48 gap-3">
              <Users size={32} style={{ color: '#d9d8d8' }} />
              <p className="text-sm" style={{ color: '#858481' }}>
                No specialists
              </p>
            </div>
          )}
          {!loading && !error &&
            items.map(item => (
              <div
                key={item.id}
                onClick={() => setSelectedId(item.id)}
                className="px-4 py-3 border-b cursor-pointer"
                style={{
                  borderColor: '#f3f1ee',
                  background: selectedId === item.id ? '#f3f1ee' : 'white',
                  borderLeft:
                    selectedId === item.id
                      ? '3px solid #eb881f'
                      : '3px solid transparent',
                }}
              >
                <div className="flex items-center justify-between">
                  <span
                    className="text-sm font-semibold truncate"
                    style={{ color: '#142234' }}
                  >
                    {item.display_name}
                  </span>
                </div>
                <div
                  className="text-xs mt-0.5 truncate"
                  style={{ color: '#858481' }}
                >
                  {item.model}
                </div>
              </div>
            ))}
        </div>

        <div
          className="px-4 py-2 border-t text-xs"
          style={{ borderColor: '#d9d8d8', color: '#858481' }}
        >
          {items.length} total
        </div>
      </div>

      {/* Right pane */}
      {selectedId === null ? (
        <div
          className="flex-1 flex items-center justify-center"
          style={{ color: '#858481' }}
        >
          <div className="text-center">
            <Users
              size={40}
              style={{ color: '#d9d8d8', margin: '0 auto 12px' }}
            />
            <p className="text-sm">Select a specialist to view its prompt.</p>
          </div>
        </div>
      ) : (
        <div
          className="flex-1 overflow-y-auto p-6"
          style={{ background: '#f3f1ee' }}
        >
          <div className="max-w-3xl">
            {promptLoading && (
              <div
                className="flex items-center justify-center h-32 text-sm"
                style={{ color: '#858481' }}
              >
                Loading…
              </div>
            )}
            {!promptLoading && promptError && (
              <div
                className="bg-white border p-4 text-sm"
                style={{ borderColor: '#d9d8d8', color: '#dc2626' }}
              >
                {promptError}
              </div>
            )}
            {!promptLoading && !promptError && prompt && (
              <>
                <div
                  className="bg-white border p-5 mb-4"
                  style={{ borderColor: '#d9d8d8' }}
                >
                  <div className="flex items-center gap-3 mb-2 flex-wrap">
                    <h2
                      className="text-xl"
                      style={{
                        color: '#142234',
                        fontFamily: 'DM Serif Display, serif',
                      }}
                    >
                      {prompt.display_name}
                    </h2>
                    <span
                      className="text-xs font-mono px-2 py-0.5"
                      style={{
                        background: 'rgba(48,128,188,0.08)',
                        color: '#3080bc',
                      }}
                    >
                      v{prompt.version_number}
                    </span>
                    <span
                      className="text-xs font-mono"
                      style={{ color: '#858481' }}
                    >
                      updated {prompt.updated_at}
                    </span>
                  </div>
                  {(() => {
                    const summary = items.find(i => i.id === prompt.id)
                    return summary && summary.description ? (
                      <p
                        className="text-sm"
                        style={{ color: '#858481' }}
                      >
                        {summary.description}
                      </p>
                    ) : null
                  })()}
                </div>

                {!canEdit && (
                  <div
                    className="border p-3 mb-4 text-sm"
                    style={{
                      background: '#f3f1ee',
                      borderColor: '#d9d8d8',
                      color: '#858481',
                    }}
                  >
                    Read only. Only owners and principals can edit specialist
                    prompts.
                  </div>
                )}

                <div
                  className="bg-white border p-5 mb-4"
                  style={{ borderColor: '#d9d8d8' }}
                >
                  <label
                    className="text-xs font-bold uppercase tracking-wider block mb-2"
                    style={{ color: '#858481' }}
                  >
                    Prompt body
                  </label>
                  <textarea
                    aria-label="Prompt body"
                    className="w-full text-sm p-3 border font-mono resize-y focus:outline-none"
                    style={{
                      borderColor: '#3080bc',
                      color: '#34322d',
                      minHeight: '60vh',
                    }}
                    value={draftText}
                    readOnly={!canEdit}
                    onChange={e => {
                      if (canEdit) {
                        setDraftText(e.target.value)
                        setSavedVersion(null)
                      }
                    }}
                  />
                </div>

                {canEdit && (
                  <div
                    className="bg-white border p-5"
                    style={{ borderColor: '#d9d8d8' }}
                  >
                    <label
                      className="text-xs font-bold uppercase tracking-wider block mb-2"
                      style={{ color: '#858481' }}
                      htmlFor="change-summary"
                    >
                      Change summary
                    </label>
                    <input
                      id="change-summary"
                      type="text"
                      className="input"
                      placeholder="Describe this change (required, min 10 chars)"
                      value={changeSummary}
                      onChange={e => {
                        setChangeSummary(e.target.value)
                        setSavedVersion(null)
                      }}
                    />
                    <div
                      className="text-xs mt-1"
                      style={{
                        color: summaryValid ? '#858481' : '#eb881f',
                      }}
                    >
                      {changeSummary.trim().length}/{MIN_SUMMARY_LEN}
                    </div>

                    {dirty && !savedVersion && (
                      <div
                        className="text-xs mt-3"
                        style={{ color: '#eb881f' }}
                      >
                        Unsaved changes
                      </div>
                    )}
                    {saveError && (
                      <div
                        className="text-sm mt-3"
                        style={{ color: '#dc2626' }}
                      >
                        {saveError}
                      </div>
                    )}
                    {savedVersion !== null && !dirty && (
                      <div
                        className="text-sm mt-3 flex items-center gap-1"
                        style={{ color: '#16a34a' }}
                      >
                        <CheckCircle size={12} /> Saved as v{savedVersion}
                      </div>
                    )}

                    <div className="flex justify-end mt-4">
                      <button
                        className="btn-primary flex items-center gap-2"
                        onClick={save}
                        disabled={!canSave}
                      >
                        <CheckCircle size={14} />
                        {saving ? 'Saving…' : 'Save'}
                      </button>
                    </div>
                  </div>
                )}
              </>
            )}
          </div>
        </div>
      )}
    </div>
  )
}
