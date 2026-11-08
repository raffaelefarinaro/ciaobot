import { describe, expect, it } from 'vitest'
import {
  actionCopy,
  callbackRefusalCopy,
  chatActionErrorReason,
  isChatAction,
  isGoogleConsentUrl,
  looksLikeLoopbackCallback,
  normalizeChatActions,
  restartTarget,
  SYSTEM_SETTING_TITLES,
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
    ['open', 'Sign in to linear', 'linear is set up for this chat but not signed in. Sign in once so this chat can use its tools.', 'Sign in'],
    ['working', 'Starting the sign-in to linear…', 'Getting the sign-in page.', ''],
    ['waiting', 'Waiting for linear to connect…', 'Finish the sign-in in the page that opened. This card updates by itself.', 'Open the sign-in page again'],
    ['done', 'linear is connected', 'This chat can use it now.', 'Continue the chat'],
    ['failed', "Couldn't sign in to linear", 'The sign-in did not finish.', 'Try again'],
  ] as const)('gives the mcp_auth copy for %s', (state, title, why, primaryLabel) => {
    const copy = actionCopy(action({ state }))
    expect(copy).toEqual({ title, why, primaryLabel, meta: 'opencode · MCP' })
  })

  it('gives the Claude sign-in copy while waiting', () => {
    const claude = actionCopy(action({
      state: 'waiting',
      params: { server: 'notion', provider: 'claude', url: 'https://x' },
    }))
    expect(claude?.primaryLabel).toBe('Open the sign-in page')
    expect(claude?.why).toContain('finish there')
    const opencode = actionCopy(action({
      state: 'waiting',
      params: { server: 'notion', provider: 'opencode', url: 'https://x' },
    }))
    expect(opencode?.primaryLabel).toBe('Open the sign-in page again')
  })

  it('says a Claude sign-in is starting a terminal', () => {
    const copy = actionCopy(action({
      state: 'working',
      params: { server: 'notion', provider: 'claude' },
    }))
    expect(copy?.why).toBe('Starting a sign-in terminal for this chat.')
  })

  it('shows the engine error for a failed sign-in, and a default without one', () => {
    expect(actionCopy(action({ state: 'failed', error: 'Timed out waiting for sign-in' }))?.why)
      .toBe('Timed out waiting for sign-in')
    expect(actionCopy(action({ state: 'failed', error: '' }))?.why).toBe('The sign-in did not finish.')
  })

  it('gives the device_code copy for gh and az', () => {
    expect(actionCopy(action({ kind: 'device_code', params: { tool: 'gh' } }))).toEqual({
      title: 'Sign in to GitHub CLI',
      why: 'The agent needs gh signed in to continue.',
      primaryLabel: 'Start sign-in',
      meta: 'gh · device code',
    })
    expect(actionCopy(action({ kind: 'device_code', params: { tool: 'az' } }))?.title).toBe('Sign in to Azure CLI')
  })

  it('names an unknown device_code tool by itself', () => {
    expect(actionCopy(action({ kind: 'device_code', params: { tool: 'aws' } }))?.title).toBe('Sign in to aws')
  })

  it('gives the device_code copy while waiting and once done', () => {
    const waiting = actionCopy(action({ kind: 'device_code', state: 'waiting', params: { tool: 'gh' } }))
    expect(waiting?.primaryLabel).toBe('')
    expect(waiting?.why).toContain('The card updates by itself')
    const done = actionCopy(action({ kind: 'device_code', state: 'done', params: { tool: 'gh' } }))
    expect(done?.primaryLabel).toBe('Continue the chat')
  })

  it('shows the engine error for a failed device_code sign-in', () => {
    const failed = actionCopy(action({
      kind: 'device_code',
      state: 'failed',
      params: { tool: 'gh' },
      error: 'gh sign-in ended with exit code 1.',
    }))
    expect(failed?.why).toBe('gh sign-in ended with exit code 1.')
    expect(failed?.primaryLabel).toBe('Try again')
    expect(actionCopy(action({ kind: 'device_code', state: 'failed', params: { tool: 'gh' }, error: '' }))?.why)
      .toBe('The sign-in did not finish.')
  })

  it('gives the command copy for open, with the default why', () => {
    const copy = actionCopy(action({ kind: 'command', params: { command: 'ls' } }))
    expect(copy).toEqual({
      title: 'Suggested command — review before running',
      why: "The assistant suggested this. Ciao has not checked it and won't run it for you.",
      primaryLabel: 'Type it in a terminal',
      meta: 'Suggested command',
    })
  })

  it('shows the note as the why of an open command', () => {
    const copy = actionCopy(action({ kind: 'command', params: { command: 'bq ls', note: 'Signs you in to BigQuery' } }))
    expect(copy?.why).toBe('Signs you in to BigQuery')
  })

  it('gives the command copy for working and done', () => {
    const working = actionCopy(action({ kind: 'command', state: 'working', params: { command: 'ls' } }))
    expect(working).toEqual({
      title: 'Review it, then press Enter in the terminal',
      why: 'The command is typed at the prompt. Edit it if you want. Nothing has run.',
      primaryLabel: 'I ran it',
      meta: 'Suggested command',
    })
    expect(actionCopy(action({ kind: 'command', state: 'done', params: { command: 'ls' } }))?.primaryLabel).toBe('Continue')
  })

  it('is null for a kind with no copy yet', () => {
    expect(actionCopy(action({ kind: 'not_a_kind' as never }))).toBeNull()
  })
})

describe('restart copy', () => {
  it('sends an update to General and a setting to This host', () => {
    expect(restartTarget('update')).toBe('/settings#updates')
    expect(restartTarget('config')).toBe('/settings/advanced#restart-host')
  })

  it('sends an unknown reason to This host', () => {
    expect(restartTarget('mcp')).toBe('/settings/advanced#restart-host')
  })

  it('words the config why as the app speaking, not the user', () => {
    const copy = actionCopy(action({ kind: 'restart', params: { reason: 'config' } }))
    expect(copy?.why).toContain('Ciao changed a setting')
    expect(copy?.why).not.toContain('you changed')
  })

  it('says every reason waits for running chats', () => {
    for (const reason of ['config', 'update']) {
      const copy = actionCopy(action({ kind: 'restart', params: { reason } }))
      expect(copy?.why).toContain('waits for running chats to finish')
    }
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

  it('keeps the bypass card copy to the mode name and what it allows', () => {
    const copy = actionCopy(action({ kind: 'permission_mode', params: { mode: 'bypass' } }))
    expect(copy?.why).toBe('Bypass: allow everything.')
    expect(copy?.why).not.toContain('without asking')
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

describe('system_setting copy', () => {
  const setting = (over: Partial<ChatAction> = {}) =>
    action({ kind: 'system_setting', origin: 'agent', params: { setting: 'full_disk_access' }, ...over })

  it('asks for the pane on the Mac, with the host app to turn on', () => {
    const copy = actionCopy(setting())
    expect(copy?.title).toBe('Allow Full Disk Access in System Settings')
    expect(copy?.why).toContain('Ciaobot Server')
    expect(copy?.primaryLabel).toBe('Open System Settings')
    expect(copy?.meta).toBe('macOS · Privacy & Security')
  })

  it('shows the agent note as the reason', () => {
    const copy = actionCopy(setting({ params: { setting: 'full_disk_access', note: 'to read Mail' } }))
    expect(copy?.why.startsWith('to read Mail')).toBe(true)
  })

  it('tells a done Full Disk Access card to restart from Settings', () => {
    const copy = actionCopy(setting({ state: 'done' }))
    expect(copy?.why).toContain('Restart it from Settings')
    expect(copy?.primaryLabel).toBe('Continue the chat')
  })

  it('tells a done card for another pane to continue', () => {
    const copy = actionCopy(setting({ state: 'done', params: { setting: 'camera' } }))
    expect(copy?.why).toBe('Continue tells the agent to try again.')
    expect(copy?.primaryLabel).toBe('Continue the chat')
  })

  it('shows the error of a failed open, with Try again', () => {
    const copy = actionCopy(setting({ state: 'failed', error: 'boom', params: { setting: 'camera' } }))
    expect(copy?.title).toBe('System Settings did not open')
    expect(copy?.why).toBe('boom')
    expect(copy?.primaryLabel).toBe('Try again')
  })

  it('gives no Open for a setting the PWA does not know', () => {
    const copy = actionCopy(setting({ params: { setting: 'nope' } }))
    expect(copy?.title).toBe('Change a macOS privacy setting')
    expect(copy?.primaryLabel).toBe('')
  })

  it('has the ten engine titles and no others', () => {
    expect(Object.keys(SYSTEM_SETTING_TITLES).sort()).toEqual([
      'accessibility', 'automation', 'calendars', 'camera', 'contacts', 'full_disk_access',
      'microphone', 'photos', 'reminders', 'screen_recording',
    ])
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

describe('looksLikeLoopbackCallback', () => {
  it('accepts the Claude and OpenCode loopback callbacks', () => {
    expect(looksLikeLoopbackCallback('http://localhost:62471/callback?code=a')).toBe(true)
    expect(looksLikeLoopbackCallback('http://127.0.0.1:56446/callback?code=a')).toBe(true)
    expect(looksLikeLoopbackCallback('  http://LOCALHOST:1234/x  ')).toBe(true)
  })

  it('refuses anything else', () => {
    expect(looksLikeLoopbackCallback('https://localhost:1/x')).toBe(false)
    expect(looksLikeLoopbackCallback('http://example.com:1/x')).toBe(false)
    expect(looksLikeLoopbackCallback('http://localhost/callback')).toBe(false)
    expect(looksLikeLoopbackCallback('')).toBe(false)
  })
})

describe('callbackRefusalCopy', () => {
  it('maps the refusal words', () => {
    expect(callbackRefusalCopy('state_mismatch')).toContain('different sign-in')
    expect(callbackRefusalCopy('wrong_port')).toContain('not the address')
    expect(callbackRefusalCopy('invalid_state')).toContain('no longer waiting')
    expect(callbackRefusalCopy('already_used')).toContain('already received')
  })

  it('has a default for any other reason', () => {
    expect(callbackRefusalCopy('invalid_body')).toBe('That address could not be used.')
    expect(callbackRefusalCopy('nonsense')).toBe('That address could not be used.')
  })
})

describe('gws_auth copy', () => {
  const gws = (over: Partial<ChatAction> = {}): ChatAction =>
    action({ kind: 'gws_auth', params: { profile: 'work' }, ...over })

  it('titles an open sign-in and names the profile in the why', () => {
    const copy = actionCopy(gws({ state: 'open' }))
    expect(copy?.title).toBe('Sign in to Google again')
    expect(copy?.why).toContain('work')
    expect(copy?.primaryLabel).toBe('Sign in with Google')
  })

  it('continues the chat once the sign-in is done', () => {
    expect(actionCopy(gws({ state: 'done' }))?.primaryLabel).toBe('Continue the chat')
  })

  it('shows the engine error as the why of a failed sign-in', () => {
    const copy = actionCopy(gws({ state: 'failed', error: 'X' }))
    expect(copy?.why).toBe('X')
    expect(copy?.primaryLabel).toBe('Try again')
  })

  it('has plain copy for the two new paste-back refusals', () => {
    expect(callbackRefusalCopy('no_pending_sign_in')).toContain('No Google sign-in is waiting')
    expect(callbackRefusalCopy('bad_callback_url')).toContain('http://127.0.0.1:')
  })

  it('accepts only an https accounts.google.com consent URL', () => {
    expect(isGoogleConsentUrl('https://accounts.google.com/o/oauth2/auth?a=1')).toBe(true)
    expect(isGoogleConsentUrl('http://accounts.google.com/x')).toBe(false)
    expect(isGoogleConsentUrl('https://accounts.google.com.evil.io/x')).toBe(false)
    expect(isGoogleConsentUrl('javascript:alert(1)')).toBe(false)
    expect(isGoogleConsentUrl(undefined)).toBe(false)
  })
})
