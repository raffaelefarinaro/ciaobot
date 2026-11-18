/**
 * Bridge to Ciaobot for Mac's native settings (the menu bar's Settings
 * submenu). The Mac app registers the `ciaobotAppSettings` script message
 * handler only in its connected main window, so the handler is absent in a
 * browser, an installed PWA and an older Mac app. This module is the only
 * reader of that handler name; nothing else may touch `webkit.messageHandlers`.
 *
 * Every string in the reply is native-authored and rendered as text. The reply
 * is validated strictly: anything that is not the v1 shape reads as null.
 */

export type MacNotificationState = 'off' | 'on' | 'denied' | 'deliveryOff' | 'unavailable'
export type MacNotificationAction = 'toggle' | 'openSystemSettings' | 'none'

export interface MacNotificationSettings {
  state: MacNotificationState
  ready: boolean
  action: MacNotificationAction
  title: string
  detail: string
}

export interface MacAppSettings {
  v: 1
  notifications: MacNotificationSettings | null
}

/** Dispatched on `window` by the Mac app when its state changes. Carries no
 *  data: the page re-reads the snapshot. */
export const MAC_APP_SETTINGS_CHANGED = 'ciaobot-app-settings-changed'

const HANDLER = 'ciaobotAppSettings'
const MAX_STRING = 300
/** The only state/action pairs the app can send; any other pair is a
 *  contradiction (a switch on an unavailable row, say) and reads as null. */
const ACTION_FOR_STATE: Readonly<Record<MacNotificationState, MacNotificationAction>> = {
  off: 'toggle',
  on: 'toggle',
  denied: 'openSystemSettings',
  deliveryOff: 'openSystemSettings',
  unavailable: 'none',
}
// C0/C1 controls plus the bidi marks, embeddings, overrides and isolates.
const UNSAFE_CHARS = /[\u0000-\u001F\u007F-\u009F\u061C\u200E\u200F\u202A-\u202E\u2066-\u2069]/g

interface Handler {
  postMessage(request: { op: string }): Promise<unknown>
}

function handler(): Handler | null {
  const webkit = (globalThis as { webkit?: { messageHandlers?: Record<string, Handler | undefined> } }).webkit
  return webkit?.messageHandlers?.[HANDLER] ?? null
}

export function hasMacAppSettings(): boolean {
  return handler() !== null
}

function cap(value: unknown): string | null {
  if (typeof value !== 'string') return null
  const clean = value.replace(UNSAFE_CHARS, '')
  if (clean.length <= MAX_STRING) return clean
  const last = clean.charCodeAt(MAX_STRING - 1)
  // Do not cut between the halves of a surrogate pair.
  const end = last >= 0xd800 && last <= 0xdbff ? MAX_STRING - 1 : MAX_STRING
  return clean.slice(0, end)
}

function parseNotifications(raw: unknown): MacNotificationSettings | null | undefined {
  if (raw === null) return null
  if (typeof raw !== 'object') return undefined
  const n = raw as Record<string, unknown>
  const title = cap(n.title)
  const detail = cap(n.detail)
  if (typeof n.state !== 'string' || !Object.hasOwn(ACTION_FOR_STATE, n.state)) return undefined
  if (ACTION_FOR_STATE[n.state as MacNotificationState] !== n.action) return undefined
  if (typeof n.ready !== 'boolean' || title === null || detail === null) return undefined
  if (title.trim() === '') return undefined
  return {
    state: n.state as MacNotificationState,
    ready: n.ready,
    action: n.action as MacNotificationAction,
    title,
    detail,
  }
}

function parse(raw: unknown): MacAppSettings | null {
  if (typeof raw !== 'object' || raw === null) return null
  const r = raw as Record<string, unknown>
  if (r.v !== 1) return null
  const notifications = parseNotifications(r.notifications)
  if (notifications === undefined) return null
  return { v: 1, notifications }
}

async function send(op: 'get' | 'notifications'): Promise<MacAppSettings | null> {
  const h = handler()
  if (!h) return null
  try {
    return parse(await h.postMessage({ op }))
  } catch {
    return null
  }
}

/** The current snapshot, or null: no handler, a refused request, or a reply
 *  that is not the v1 shape. */
export function readMacAppSettings(): Promise<MacAppSettings | null> {
  return send('get')
}

/** Flip the tray's notification row (or open its System Settings pane). Must
 *  run inside a user gesture; the app refuses it otherwise. */
export function requestMacNotificationAction(): Promise<MacAppSettings | null> {
  return send('notifications')
}
