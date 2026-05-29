import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { cleanup, render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'

import Specialists from './Specialists'
import type {
  CurrentUser,
  SpecialistListResponse,
  SpecialistPromptResponse,
} from '../api/client'

vi.mock('../api/client', async () => {
  const actual = await vi.importActual<typeof import('../api/client')>(
    '../api/client',
  )
  return {
    ...actual,
    specialists: {
      list: vi.fn(),
      getPrompt: vi.fn(),
      updatePrompt: vi.fn(),
    },
  }
})

vi.mock('../auth/AuthContext', () => ({
  useAuth: vi.fn(),
}))

import { specialists } from '../api/client'
import { useAuth } from '../auth/AuthContext'

const listMock = vi.mocked(specialists.list)
const getPromptMock = vi.mocked(specialists.getPrompt)
const updatePromptMock = vi.mocked(specialists.updatePrompt)
const useAuthMock = vi.mocked(useAuth)

function makeUser(role: CurrentUser['role']): CurrentUser {
  return {
    user_id: 'user-1',
    firm_id: 'firm-1',
    firm_slug: 'mc-s-accountants',
    upn: 'test@mcands.com.au',
    display_name: 'Test User',
    role,
  }
}

const SAMPLE_LIST: SpecialistListResponse = {
  specialists: [
    {
      id: 'spec-cgt',
      name: 'cgt_concessions',
      display_name: 'CGT Concessions and Rollovers',
      description: 'Capital gains tax small business concessions',
      is_enabled: true,
      model: 'claude-opus-4-7',
      extended_thinking: true,
      active_version_id: 'v-cgt',
      updated_at: '2026-05-20T00:00:00Z',
    },
    {
      id: 'spec-div7a',
      name: 'division_7a',
      display_name: 'Division 7A',
      description: 'Private company loans to shareholders',
      is_enabled: true,
      model: 'claude-opus-4-7',
      extended_thinking: true,
      active_version_id: 'v-div7a',
      updated_at: '2026-05-20T00:00:00Z',
    },
    {
      id: 'spec-gst',
      name: 'gst',
      display_name: 'GST',
      description: 'Goods and services tax',
      is_enabled: true,
      model: 'claude-opus-4-7',
      extended_thinking: true,
      active_version_id: 'v-gst',
      updated_at: '2026-05-20T00:00:00Z',
    },
    {
      id: 'spec-smsf',
      name: 'smsf',
      display_name: 'SMSF',
      description: 'Self-managed super funds',
      is_enabled: true,
      model: 'claude-opus-4-7',
      extended_thinking: true,
      active_version_id: 'v-smsf',
      updated_at: '2026-05-20T00:00:00Z',
    },
    {
      id: 'spec-trust',
      name: 'trust_tax',
      display_name: 'Trust Tax',
      description: 'Trust taxation in Australia',
      is_enabled: true,
      model: 'claude-opus-4-7',
      extended_thinking: true,
      active_version_id: 'v-trust',
      updated_at: '2026-05-20T00:00:00Z',
    },
  ],
}

const GST_PROMPT: SpecialistPromptResponse = {
  id: 'spec-gst',
  name: 'gst',
  display_name: 'GST',
  prompt_text: 'You are the GST specialist. Help with GST questions.',
  version_number: 3,
  updated_at: '2026-05-20T00:00:00Z',
}

afterEach(() => {
  cleanup()
})

beforeEach(() => {
  listMock.mockReset()
  getPromptMock.mockReset()
  updatePromptMock.mockReset()
  useAuthMock.mockReset()

  listMock.mockResolvedValue(SAMPLE_LIST)
  getPromptMock.mockResolvedValue(GST_PROMPT)
  updatePromptMock.mockResolvedValue({
    ...GST_PROMPT,
    prompt_text: 'edited body',
    version_number: 4,
  })
})

describe('SpecialistsPage', () => {
  it('renders all specialists in the list', async () => {
    useAuthMock.mockReturnValue({
      user: makeUser('owner'),
      loading: false,
      logout: vi.fn(),
    })

    render(<Specialists />)

    for (const s of SAMPLE_LIST.specialists) {
      expect(await screen.findByText(s.display_name)).toBeInTheDocument()
    }
  })

  it('is read-only for non-privileged role', async () => {
    useAuthMock.mockReturnValue({
      user: makeUser('accountant'),
      loading: false,
      logout: vi.fn(),
    })
    const user = userEvent.setup()

    render(<Specialists />)

    await user.click(await screen.findByText('GST'))

    const textarea = (await screen.findByLabelText(
      'Prompt body',
    )) as HTMLTextAreaElement
    expect(textarea).toHaveAttribute('readonly')

    expect(
      screen.getByText(/Only owners and principals can edit/i),
    ).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: /Save/i })).toBeNull()
  })

  it('keeps Save disabled until the textarea changes and summary has 10+ chars', async () => {
    useAuthMock.mockReturnValue({
      user: makeUser('owner'),
      loading: false,
      logout: vi.fn(),
    })
    const user = userEvent.setup()

    render(<Specialists />)

    await user.click(await screen.findByText('GST'))

    const textarea = (await screen.findByLabelText(
      'Prompt body',
    )) as HTMLTextAreaElement
    const saveBtn = screen.getByRole('button', { name: /Save/i })
    expect(saveBtn).toBeDisabled()

    await user.type(textarea, ' tweak')
    expect(saveBtn).toBeDisabled() // no summary yet

    const summary = screen.getByPlaceholderText(
      /Describe this change/i,
    ) as HTMLInputElement
    await user.type(summary, 'too short')
    expect(saveBtn).toBeDisabled()

    await user.type(summary, ' enough now')
    expect(saveBtn).toBeEnabled()
  })

  it('calls updatePrompt with the typed payload on Save', async () => {
    useAuthMock.mockReturnValue({
      user: makeUser('owner'),
      loading: false,
      logout: vi.fn(),
    })
    const user = userEvent.setup()

    render(<Specialists />)

    await user.click(await screen.findByText('GST'))

    const textarea = (await screen.findByLabelText(
      'Prompt body',
    )) as HTMLTextAreaElement
    await user.type(textarea, ' extra')

    const summary = screen.getByPlaceholderText(
      /Describe this change/i,
    ) as HTMLInputElement
    await user.type(summary, 'tightening wording')

    await user.click(screen.getByRole('button', { name: /Save/i }))

    await waitFor(() => expect(updatePromptMock).toHaveBeenCalledTimes(1))
    expect(updatePromptMock).toHaveBeenCalledWith('spec-gst', {
      prompt_text: GST_PROMPT.prompt_text + ' extra',
      change_summary: 'tightening wording',
    })
  })
})
