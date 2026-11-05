import { describe, expect, it } from 'vitest'
import {
  actionCopy,
  chatActionErrorReason,
  isChatAction,
  normalizeChatActions,
  safeExternalUrl,
} from './chatActions'
import type { ChatAction } from './types'

function action(over: Partial<ChatAction> = {}): ChatAction {
  return {
    id: 'a1',
    kind: 'mcp_auth',
    origin: 'engine',
    state: 'open',
    params: { server: 'linear', provider: 'opencode' },
    error: '',
    created_at: 1,
    ...over,
  }
}

describe('chatActionErrorReason', () => {
  it('reads the engine reason', () => {
    expect(chatActionErrorReason({ status: 400, payload: { error: 'not_local' } })).toBe('not_local')
  })

  it('gives an empty reason for anything without a string reason', () => {
    expect(chatActionErrorReason(new Error('x'))).toBe('')
    expect(chatActionErrorReason(undefined)).toBe('')
    expect(chatActionErrorReason({ payload: { error: 3 } })).toBe('')
  })
})

describe('isChatAction', () => {
  it('accepts a well-formed action', () => {
    expect(isChatAction(action())).toBe(true)
  })

  it.each([
    ['an empty id', { id: '' }],
    ['an unknown kind', { kind: 'nope' }],
    ['an unknown state', { state: 'gone' }],
    ['an unknown origin', { origin: 'x' }],
    ['a non-string param value', { params: { server: 3 } }],
    ['params as an array', { params: ['linear'] }],
    ['a null error', { error: null }],
    ['a string created_at', { created_at: '1' }],
  ])('rejects %s', (_label, over) => {
    expect(isChatAction({ ...action(), ...over })).toBe(false)
  })
})

describe('normalizeChatActions', () => {
  it.each([undefined, {}, 'x'])('returns [] for a non-array (%s)', (value) => {
    expect(normalizeChatActions(value)).toEqual([])
  })

  it('drops malformed entries and copies params', () => {
    const input = [action(), { id: '' }]
    const result = normalizeChatActions(input)
    expect(result).toHaveLength(1)
    result[0].params.server = 'changed'
    expect((input[0] as ChatAction).params.server).toBe('linear')
  })
})

describe('actionCopy', () => {
  it.each([
    ['open', 'Sign in to linear', 'It is set up but not signed in. Sign in once and this chat can use it.', 'Sign in'],
    ['working', 'Starting the sign-in to linear…', 'Getting the sign-in page.', ''],
    ['waiting', 'Waiting for linear to connect…', 'Finish the sign-in in the page that opened. This card updates by itself.', 'Open the sign-in page again'],
    ['done', 'linear is connected', 'This chat can use it now.', 'Continue the chat'],
    ['failed', "Sign-in to linear didn't finish", 'The sign-in did not finish.', 'Try again'],
  ] as const)('gives the mcp_auth copy for %s', (state, title, why, primaryLabel) => {
    const copy = actionCopy(action({ state }))
    expect(copy).toEqual({ title, why, primaryLabel, meta: 'opencode · MCP' })
  })

  it('shows the engine error for a failed sign-in, and a default without one', () => {
    expect(actionCopy(action({ state: 'failed', error: 'Timed out waiting for sign-in' }))?.why)
      .toBe('Timed out waiting for sign-in')
    expect(actionCopy(action({ state: 'failed', error: '' }))?.why).toBe('The sign-in did not finish.')
  })

  it('is null for a kind with no copy yet', () => {
    expect(actionCopy(action({ kind: 'restart' }))).toBeNull()
  })

  it('gives the permission_mode copy for an open request', () => {
    const copy = actionCopy(action({ kind: 'permission_mode', params: { mode: 'normal' } }))
    expect(copy).toEqual({
      title: 'Switch this chat to Manual?',
      why: 'Manual: ask for every action.',
      primaryLabel: 'Switch to Manual',
      meta: 'Permission mode',
    })
  })

  it('warns in the bypass copy that nothing asks first', () => {
    const copy = actionCopy(action({ kind: 'permission_mode', params: { mode: 'bypass' } }))
    expect(copy?.why).toContain('without asking')
    expect(copy?.primaryLabel).toBe('Switch to Bypass')
  })

  it('says the mode applies from the next turn once it is done', () => {
    const copy = actionCopy(action({ kind: 'permission_mode', state: 'done', params: { mode: 'auto' } }))
    expect(copy).toEqual({
      title: 'Switched to Auto. It applies from the next turn.',
      why: 'Continue starts that turn.',
      primaryLabel: 'Continue the chat',
      meta: 'Permission mode',
    })
  })

  it('shows the engine error for a failed change', () => {
    const copy = actionCopy(action({ kind: 'permission_mode', state: 'failed', error: 'The mode could not be changed.', params: { mode: 'plan' } }))
    expect(copy?.why).toBe('The mode could not be changed.')
    expect(copy?.primaryLabel).toBe('Switch to Plan')
  })

  it('gives no Start for a mode the PWA does not know', () => {
    const copy = actionCopy(action({ kind: 'permission_mode', params: { mode: 'manual' } }))
    expect(copy?.title).toBe('Permission change requested')
    expect(copy?.primaryLabel).toBe('')
  })
})

describe('safeExternalUrl', () => {
  it('allows only http and https', () => {
    expect(safeExternalUrl('https://a.example/x')).toBe('https://a.example/x')
    expect(safeExternalUrl('http://127.0.0.1:5/cb')).toBe('http://127.0.0.1:5/cb')
    expect(safeExternalUrl('javascript:alert(1)')).toBeNull()
    expect(safeExternalUrl('not a url')).toBeNull()
    expect(safeExternalUrl(undefined)).toBeNull()
  })
})
