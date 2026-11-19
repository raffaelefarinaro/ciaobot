import { afterEach, describe, expect, it, vi } from 'vitest'
import {
  MAC_APP_SETTINGS_CHANGED,
  hasMacAppSettings,
  readMacAppSettings,
  requestMacAutomaticUpdates,
  requestMacCheckForUpdates,
  requestMacNotificationAction,
} from './macAppSettings'

const g = globalThis as Record<string, unknown>

function install(postMessage: (req: unknown) => unknown) {
  g.webkit = { messageHandlers: { ciaobotAppSettings: { postMessage: vi.fn(postMessage) } } }
  return (g.webkit as { messageHandlers: { ciaobotAppSettings: { postMessage: ReturnType<typeof vi.fn> } } })
    .messageHandlers.ciaobotAppSettings.postMessage
}

const note = { state: 'on', ready: true, action: 'toggle', title: 'Chat Notifications: On', detail: 'd' }
const upd = { available: true, automatic: true, canCheck: true, reason: '' }

afterEach(() => {
  delete g.webkit
})

describe('macAppSettings', () => {
  it('has the change event name the app dispatches', () => {
    expect(MAC_APP_SETTINGS_CHANGED).toBe('ciaobot-app-settings-changed')
  })

  it('reads as absent without a handler', async () => {
    expect(hasMacAppSettings()).toBe(false)
    expect(await readMacAppSettings()).toBeNull()
    expect(await requestMacNotificationAction()).toBeNull()
  })

  it('parses a valid snapshot and sends op get', async () => {
    const post = install(async () => ({ v: 1, notifications: note }))
    expect(hasMacAppSettings()).toBe(true)
    expect(await readMacAppSettings()).toEqual({ v: 1, notifications: note, updates: null })
    expect(post).toHaveBeenCalledWith({ op: 'get' })
  })

  it('accepts a null notifications block', async () => {
    install(async () => ({ v: 1, notifications: null }))
    expect(await readMacAppSettings()).toEqual({ v: 1, notifications: null, updates: null })
  })

  it.each([
    ['wrong version', { v: 2, notifications: note }],
    ['unknown state', { v: 1, notifications: { ...note, state: 'weird' } }],
    ['unknown action', { v: 1, notifications: { ...note, action: 'weird' } }],
    ['non-string title', { v: 1, notifications: { ...note, title: 3 } }],
    ['non-string detail', { v: 1, notifications: { ...note, detail: null } }],
    ['non-boolean ready', { v: 1, notifications: { ...note, ready: 'yes' } }],
    ['missing notifications', { v: 1 }],
    ['not an object', 'refused'],
    ['null', null],
    ['array reply', [{ v: 1, notifications: note }]],
    ['notifications as array', { v: 1, notifications: [note] }],
    ['notifications as string', { v: 1, notifications: 'on' }],
    ['toggle action on an unavailable state', { v: 1, notifications: { ...note, state: 'unavailable', action: 'toggle' } }],
    ['none action on an on state', { v: 1, notifications: { ...note, action: 'none' } }],
    ['openSystemSettings on an off state', { v: 1, notifications: { ...note, state: 'off', action: 'openSystemSettings' } }],
    ['toggle action on a denied state', { v: 1, notifications: { ...note, state: 'denied', action: 'toggle' } }],
    ['empty title', { v: 1, notifications: { ...note, title: '' } }],
    ['whitespace title', { v: 1, notifications: { ...note, title: '  \t ' } }],
    ['title of only control characters', { v: 1, notifications: { ...note, title: '\u0001\u202E' } }],
    ['inherited state name', { v: 1, notifications: { ...note, state: 'toString' } }],
  ])('treats %s as null', async (_label, reply) => {
    install(async () => reply)
    expect(await readMacAppSettings()).toBeNull()
  })

  it('truncates long strings to 300 characters', async () => {
    install(async () => ({ v: 1, notifications: { ...note, title: 'x'.repeat(500), detail: 'y'.repeat(500) } }))
    const out = await readMacAppSettings()
    expect(out?.notifications?.title).toHaveLength(300)
    expect(out?.notifications?.detail).toHaveLength(300)
  })

  it.each([
    ['on', 'toggle'], ['off', 'toggle'],
    ['denied', 'openSystemSettings'], ['deliveryOff', 'openSystemSettings'],
    ['unavailable', 'none'],
  ])('accepts the %s/%s pair', async (state, action) => {
    install(async () => ({ v: 1, notifications: { ...note, state, action } }))
    const out = await readMacAppSettings()
    expect(out?.notifications?.state).toBe(state)
    expect(out?.notifications?.action).toBe(action)
  })

  it('strips control and bidi-override characters from title and detail', async () => {
    install(async () => ({
      v: 1,
      notifications: { ...note, title: 'a\u0000b\u202Ec\u2066d\u200Fe\u0085f', detail: 'x\u0007y\u061Cz\u007F' },
    }))
    const out = await readMacAppSettings()
    expect(out?.notifications?.title).toBe('abcdef')
    expect(out?.notifications?.detail).toBe('xyz')
  })

  it('does not split a surrogate pair when truncating', async () => {
    const emoji = '\u{1F600}'
    install(async () => ({
      v: 1,
      notifications: { ...note, title: 'x'.repeat(299) + emoji, detail: 'y'.repeat(298) + emoji + 'z' },
    }))
    const out = await readMacAppSettings()
    expect(out?.notifications?.title).toBe('x'.repeat(299))
    expect(out?.notifications?.detail).toBe('y'.repeat(298) + emoji)
    expect(out?.notifications?.detail).toHaveLength(300)
  })

  it('reads a rejected promise as null', async () => {
    install(async () => { throw 'refused' })
    expect(await readMacAppSettings()).toBeNull()
  })

  it('posts exactly { op: notifications } for the action', async () => {
    const post = install(async () => ({ v: 1, notifications: note }))
    await requestMacNotificationAction()
    expect(post).toHaveBeenCalledTimes(1)
    expect(post.mock.calls[0]![0]).toEqual({ op: 'notifications' })
  })
})

describe('macAppSettings updates block', () => {
  it('parses a valid updates block', async () => {
    install(async () => ({ v: 1, notifications: null, updates: upd }))
    expect((await readMacAppSettings())?.updates).toEqual(upd)
  })

  it('parses an unavailable block with its native reason', async () => {
    const reason = 'Updates are unavailable in a debug build.'
    install(async () => ({ v: 1, notifications: null, updates: { available: false, automatic: false, canCheck: false, reason } }))
    expect((await readMacAppSettings())?.updates).toEqual({ available: false, automatic: false, canCheck: false, reason })
  })

  it('reads an absent updates key as null and keeps the notifications block', async () => {
    install(async () => ({ v: 1, notifications: note }))
    const out = await readMacAppSettings()
    expect(out?.notifications).toEqual(note)
    expect(out?.updates).toBeNull()
  })

  it.each([
    ['not an object', 'yes'],
    ['null', null],
    ['non-boolean available', { ...upd, available: 1 }],
    ['non-boolean automatic', { ...upd, automatic: 'true' }],
    ['non-boolean canCheck', { ...upd, canCheck: null }],
    ['non-string reason', { ...upd, reason: 3 }],
    ['available with a reason', { ...upd, reason: 'x' }],
    ['unavailable without a reason', { ...upd, available: false, canCheck: false, reason: '' }],
    ['unavailable with a check possible', { ...upd, available: false, canCheck: true, reason: 'x' }],
    ['unavailable with only control characters', { ...upd, available: false, canCheck: false, reason: '\u0001' }],
  ])('reads a %s updates block as null and keeps the snapshot', async (_label, updates) => {
    install(async () => ({ v: 1, notifications: note, updates }))
    const out = await readMacAppSettings()
    expect(out?.notifications).toEqual(note)
    expect(out?.updates).toBeNull()
  })

  it('strips control characters from the reason and caps it', async () => {
    install(async () => ({
      v: 1,
      notifications: null,
      updates: { available: false, automatic: false, canCheck: false, reason: 'No\u202E key.' + 'x'.repeat(400) },
    }))
    const reason = (await readMacAppSettings())?.updates?.reason ?? ''
    expect(reason.startsWith('No key.')).toBe(true)
    expect(reason).toHaveLength(300)
  })

  it('posts exactly { op: checkForUpdates } for the check', async () => {
    const post = install(async () => ({ v: 1, notifications: null, updates: upd }))
    await requestMacCheckForUpdates()
    expect(post).toHaveBeenCalledTimes(1)
    expect(post.mock.calls[0]![0]).toEqual({ op: 'checkForUpdates' })
  })

  it('posts { op: setAutomaticUpdates, on } with a strict boolean', async () => {
    const post = install(async () => ({ v: 1, notifications: null, updates: { ...upd, automatic: false } }))
    await requestMacAutomaticUpdates(false)
    expect(post.mock.calls[0]![0]).toEqual({ op: 'setAutomaticUpdates', on: false })
  })

  it('reads a refused update request as null', async () => {
    install(async () => { throw 'refused' })
    expect(await requestMacCheckForUpdates()).toBeNull()
    expect(await requestMacAutomaticUpdates(true)).toBeNull()
  })
})
