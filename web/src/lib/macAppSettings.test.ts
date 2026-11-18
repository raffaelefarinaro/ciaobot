import { afterEach, describe, expect, it, vi } from 'vitest'
import {
  MAC_APP_SETTINGS_CHANGED,
  hasMacAppSettings,
  readMacAppSettings,
  requestMacNotificationAction,
} from './macAppSettings'

const g = globalThis as Record<string, unknown>

function install(postMessage: (req: unknown) => unknown) {
  g.webkit = { messageHandlers: { ciaobotAppSettings: { postMessage: vi.fn(postMessage) } } }
  return (g.webkit as { messageHandlers: { ciaobotAppSettings: { postMessage: ReturnType<typeof vi.fn> } } })
    .messageHandlers.ciaobotAppSettings.postMessage
}

const note = { state: 'on', ready: true, action: 'toggle', title: 'Chat Notifications: On', detail: 'd' }

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
    expect(await readMacAppSettings()).toEqual({ v: 1, notifications: note })
    expect(post).toHaveBeenCalledWith({ op: 'get' })
  })

  it('accepts a null notifications block', async () => {
    install(async () => ({ v: 1, notifications: null }))
    expect(await readMacAppSettings()).toEqual({ v: 1, notifications: null })
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
