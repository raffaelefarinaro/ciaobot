/**
 * Needs-you chat actions: pure helpers, no Vue imports.
 *
 * The engine owns the action and sends its kind, state and params. The PWA
 * composes all card copy here from kind and params; the engine sends no
 * display prose except `error` and `note`.
 */
import { isBridgeMode, PERMISSION_MODE_COPY } from './permissionModes'
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
  // Claude's sign-in runs in an engine helper terminal: the URL arrives a poll later.
  const claude = action.params.provider === 'claude'
  switch (action.state) {
    case 'open':
      return {
        title: `Sign in to ${server}`,
        why: `${server} is set up for this chat but not signed in. Sign in once so this chat can use its tools.`,
        primaryLabel: 'Sign in',
        meta,
      }
    case 'working':
      return {
        title: `Starting the sign-in to ${server}…`,
        why: claude ? 'Starting a sign-in terminal for this chat.' : 'Getting the sign-in page.',
        primaryLabel: '',
        meta,
      }
    case 'waiting':
      return {
        title: `Waiting for ${server} to connect…`,
        why: claude
          ? 'Open the sign-in page and finish there. This card updates by itself.'
          : 'Finish the sign-in in the page that opened. This card updates by itself.',
        primaryLabel: claude ? 'Open the sign-in page' : 'Open the sign-in page again',
        meta,
      }
    case 'done':
      return { title: `${server} is connected`, why: 'This chat can use it now.', primaryLabel: 'Continue the chat', meta }
    case 'failed':
      return {
        title: `Couldn't sign in to ${server}`,
        why: action.error || 'The sign-in did not finish.',
        primaryLabel: 'Try again',
        meta,
      }
  }
}

/** The display name of a device-code tool. The engine allows only gh and az. */
const DEVICE_TOOL_NAMES: Record<string, string> = { gh: 'GitHub CLI', az: 'Azure CLI' }

function deviceCodeCopy(action: ChatAction): ActionCopy {
  const tool = action.params.tool ?? ''
  const name = DEVICE_TOOL_NAMES[tool] ?? tool
  const meta = `${tool} · device code`
  switch (action.state) {
    case 'open':
      return {
        title: `Sign in to ${name}`,
        why: `The agent needs ${tool} signed in to continue.`,
        primaryLabel: 'Start sign-in',
        meta,
      }
    case 'working':
      return {
        title: `Starting ${name} sign-in…`,
        why: 'Starting a sign-in terminal for this chat.',
        primaryLabel: '',
        meta,
      }
    case 'waiting':
      return {
        title: `Enter the code to sign in to ${name}`,
        why: 'Enter this code on the page, then come back. The card updates by itself.',
        primaryLabel: '',
        meta,
      }
    case 'done':
      return { title: `Signed in to ${name}`, why: `The agent can use ${tool} now.`, primaryLabel: 'Continue the chat', meta }
    case 'failed':
      return {
        title: `Couldn't sign in to ${name}`,
        why: action.error || 'The sign-in did not finish.',
        primaryLabel: 'Try again',
        meta,
      }
  }
}

function commandCopy(action: ChatAction): ActionCopy {
  const meta = 'Suggested command'
  switch (action.state) {
    case 'working':
      return {
        title: 'Review it, then press Enter in the terminal',
        why: 'The command is typed at the prompt. Edit it if you want. Nothing has run.',
        primaryLabel: 'I ran it',
        meta,
      }
    case 'done':
      return {
        title: 'You ran the command',
        why: 'Continue lets the assistant check the result.',
        primaryLabel: 'Continue',
        meta,
      }
    default:
      return {
        title: 'Suggested command — review before running',
        why: action.params.note || "The assistant suggested this. Ciao has not checked it and won't run it for you.",
        primaryLabel: 'Type it in a terminal',
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
        why: `Ciao changed a setting that takes effect after Ciaobot restarts.${RESTART_COST}`,
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

const PERMISSION_MODE_META = 'Permission mode'

function permissionModeCopy(action: ChatAction): ActionCopy {
  const mode = action.params.mode
  if (!isBridgeMode(mode)) {
    // A mode the PWA does not know is shown, never offered: no Start.
    return { title: 'Permission change requested', why: '', primaryLabel: '', meta: PERMISSION_MODE_META }
  }
  const { name, allows } = PERMISSION_MODE_COPY[mode]
  if (action.state === 'done') {
    return {
      title: `Switched to ${name}. It applies from the next turn.`,
      why: 'Continue starts that turn.',
      primaryLabel: 'Continue the chat',
      meta: PERMISSION_MODE_META,
    }
  }
  return {
    title: `Switch this chat to ${name}?`,
    why: action.state === 'failed' && action.error ? action.error : `${name}: ${allows}.`,
    primaryLabel: `Switch to ${name}`,
    meta: PERMISSION_MODE_META,
  }
}

/** The card copy for an action, or null for a kind with no copy yet. */
export function actionCopy(action: ChatAction): ActionCopy | null {
  switch (action.kind) {
    case 'mcp_auth':
      return mcpAuthCopy(action)
    case 'command':
      return commandCopy(action)
    case 'restart':
      return restartCopy(action)
    case 'permission_mode':
      return permissionModeCopy(action)
    case 'device_code':
      return deviceCodeCopy(action)
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
