/**
 * Needs-you chat actions: pure helpers, no Vue imports.
 *
 * The engine owns the action and sends its kind, state and params. The PWA
 * composes all card copy here from kind and params; the engine sends no
 * display prose except `error` and `note`.
 */
import type { ChatAction, ChatActionKind, ChatActionState } from './types'

export const CHAT_ACTION_KINDS: readonly ChatActionKind[] = [
  'mcp_auth', 'command', 'gws_auth', 'permission_mode', 'device_code', 'system_setting', 'restart', 'limit',
]

export const CHAT_ACTION_STATES: readonly ChatActionState[] = ['open', 'working', 'waiting', 'done', 'failed']

export interface ActionCopy {
  title: string
  why: string
  primaryLabel: string
  meta: string
}

/** The steps of an MCP sign-in, shown while the card is waiting. */
export const MCP_AUTH_STEPS: readonly string[] = ['Sign-in page opened', 'Sign in in the browser', 'Ciao picks up the tools']

export function isChatAction(value: unknown): value is ChatAction {
  if (typeof value !== 'object' || value === null) return false
  const v = value as Record<string, unknown>
  if (typeof v.id !== 'string' || v.id.length === 0) return false
  if (!CHAT_ACTION_KINDS.includes(v.kind as ChatActionKind)) return false
  if (!CHAT_ACTION_STATES.includes(v.state as ChatActionState)) return false
  if (v.origin !== 'engine' && v.origin !== 'agent') return false
  if (typeof v.params !== 'object' || v.params === null || Array.isArray(v.params)) return false
  if (!Object.values(v.params as Record<string, unknown>).every(x => typeof x === 'string')) return false
  return typeof v.error === 'string' && typeof v.created_at === 'number'
}

export function normalizeChatActions(value: unknown): ChatAction[] {
  return Array.isArray(value)
    ? value.filter(isChatAction).map(a => ({ ...a, params: { ...a.params } }))
    : []
}

function mcpAuthCopy(action: ChatAction): ActionCopy {
  const server = action.params.server ?? ''
  const meta = `${action.params.provider ?? ''} · MCP`
  switch (action.state) {
    case 'open':
      return {
        title: `Sign in to ${server}`,
        why: 'It is set up but not signed in. Sign in once and this chat can use it.',
        primaryLabel: 'Sign in',
        meta,
      }
    case 'working':
      return { title: `Starting the sign-in to ${server}…`, why: 'Getting the sign-in page.', primaryLabel: '', meta }
    case 'waiting':
      return {
        title: `Waiting for ${server} to connect…`,
        why: 'Finish the sign-in in the page that opened. This card updates by itself.',
        primaryLabel: 'Open the sign-in page again',
        meta,
      }
    case 'done':
      return { title: `${server} is connected`, why: 'This chat can use it now.', primaryLabel: 'Continue the chat', meta }
    case 'failed':
      return {
        title: `Sign-in to ${server} didn't finish`,
        why: action.error || 'The sign-in did not finish.',
        primaryLabel: 'Try again',
        meta,
      }
  }
}

/** Every restart card says what the restart costs, in the words Settings uses. */
const RESTART_COST = ' Restarting waits for running chats to finish, and closes any open question or approval cards.'

/** Where a restart card's button leads: the Settings control that restarts or updates. */
export function restartTarget(reason: string): string {
  return reason === 'update' ? '/settings#updates' : '/settings/advanced#restart-host'
}

function restartCopy(action: ChatAction): ActionCopy {
  const meta = 'Ciaobot · Settings'
  switch (action.params.reason) {
    case 'config':
      return {
        title: 'Restart Ciaobot to apply a setting',
        why: `A setting you changed takes effect after Ciaobot restarts.${RESTART_COST}`,
        primaryLabel: 'Open Settings',
        meta,
      }
    case 'update':
      return {
        title: 'Update Ciaobot',
        why: `An update is ready. Installing it restarts Ciaobot.${RESTART_COST}`,
        primaryLabel: 'Open Updates',
        meta,
      }
    default:
      return {
        title: 'Restart requested',
        why: `Ciaobot needs a restart to apply a change.${RESTART_COST}`,
        primaryLabel: 'Open Settings',
        meta,
      }
  }
}

/** The card copy for an action, or null for a kind with no copy yet. */
export function actionCopy(action: ChatAction): ActionCopy | null {
  switch (action.kind) {
    case 'mcp_auth':
      return mcpAuthCopy(action)
    case 'restart':
      return restartCopy(action)
    default:
      return null
  }
}

/** The engine's refusal reason from a rejected request, or '' when there is none. */
export function chatActionErrorReason(e: unknown): string {
  const err = (e ?? null) as { payload?: { error?: unknown } } | null
  return typeof err?.payload?.error === 'string' ? err.payload.error : ''
}

/** The URL when it is http or https, else null. Nothing else is ever opened or linked. */
export function safeExternalUrl(raw: unknown): string | null {
  if (typeof raw !== 'string') return null
  try {
    const url = new URL(raw)
    return url.protocol === 'https:' || url.protocol === 'http:' ? url.href : null
  } catch {
    return null
  }
}
