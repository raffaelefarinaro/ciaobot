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

/** Sparkle's view of Ciaobot for Mac's updates. `reason` is native text and
 *  is empty when `available` is true. */
export interface MacUpdateSettings {
  available: boolean
  automatic: boolean
  canCheck: boolean
  reason: string
}

export interface MacAppSettings {
  v: 1
  notifications: MacNotificationSettings | null
  /** Null when the app predates the updates key or sends no updater. */
  updates: MacUpdateSettings | null
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

function parseUpdates(raw: unknown): MacUpdateSettings | null {
  // An absent or malformed block reads as null: the Mac app predates it or
  // sent something this page does not know, and the card keeps its static
  // text. It does not make the whole snapshot unusable.
  if (typeof raw !== 'object' || raw === null) return null
  const u = raw as Record<string, unknown>
  const reason = cap(u.reason)
  if (typeof u.available !== 'boolean' || typeof u.automatic !== 'boolean' || typeof u.canCheck !== 'boolean' || reason === null) return null
  // The app sends a reason exactly when updates are unavailable, and a check
  // is possible only when they are. Anything else is not a state it sends.
  if (u.available !== (reason === '')) return null
  if (!u.available && (reason.trim() === '' || u.canCheck)) return null
  return { available: u.available, automatic: u.automatic, canCheck: u.canCheck, reason }
}

function parse(raw: unknown): MacAppSettings | null {
  if (typeof raw !== 'object' || raw === null) return null
  const r = raw as Record<string, unknown>
  if (r.v !== 1) return null
  const notifications = parseNotifications(r.notifications)
  if (notifications === undefined) return null
  return { v: 1, notifications, updates: parseUpdates(r.updates) }
}

type Request =
  | { op: 'get' }
  | { op: 'notifications' }
  | { op: 'checkForUpdates' }
  | { op: 'setAutomaticUpdates'; on: boolean }

async function send(request: Request): Promise<MacAppSettings | null> {
  const h = handler()
  if (!h) return null
  try {
    return parse(await h.postMessage(request))
  } catch {
    return null
  }
}

/** The current snapshot, or null: no handler, a refused request, or a reply
 *  that is not the v1 shape. */
export function readMacAppSettings(): Promise<MacAppSettings | null> {
  return send({ op: 'get' })
}

/** Flip the tray's notification row (or open its System Settings pane). Must
 *  run inside a user gesture; the app refuses it otherwise. */
export function requestMacNotificationAction(): Promise<MacAppSettings | null> {
  return send({ op: 'notifications' })
}

/** Start Sparkle's update check, the tray's Check for Updates… row. Must run
 *  inside a user gesture. */
export function requestMacCheckForUpdates(): Promise<MacAppSettings | null> {
  return send({ op: 'checkForUpdates' })
}

/** Set the tray's Install Updates Automatically row. Must run inside a user
 *  gesture. */
export function requestMacAutomaticUpdates(on: boolean): Promise<MacAppSettings | null> {
  return send({ op: 'setAutomaticUpdates', on })
}
