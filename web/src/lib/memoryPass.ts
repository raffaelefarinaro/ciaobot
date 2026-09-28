// Reading the memory pass from the PWA.
//
// The pass runs as an ordinary chat in an app-owned per-workspace system
// project, and the backend has always serialised everything needed to recognise
// one: `ProjectInfo.kind === 'memory'` on the project, and
// `ChatInfo.helper.kind === 'memory_pass'` on the chat it created. Nothing in
// the PWA read those fields, so this is the only place that does — Vue-free, so
// the rules can be tested on their own.
//
// Four facts the UI needs, each deliberately narrow:
//
//   - *is this a pass* — keyed on `kind`, never on `is_system` or a project
//     name. A user project may legitimately be called "Memory"; only the kind
//     is the discriminator.
//   - *where is it in its lifecycle* — the normalised `state`, so the pass's
//     own queue position is read from one field rather than re-derived from
//     the same state a second time elsewhere.
//   - *does it need the owner* — `state === 'attention'`. A pass that ends
//     cleanly auto-archives and never reaches Home, so this one state is the
//     only thing worth interrupting anyone about.
//   - *which chat is the pass for an archived source* — the id recorded on the
//     source's own `postprocess` record. It is written directly (not folded in
//     from the archive job) because the pass outlives the job by as long as the
//     pass takes.
//
// The pass is deliberately not an ordinary chat anywhere in the UI: it is
// listed only as one entry in the memory-insight rail Home derives from it
// (`memoryInsights.ts`), and reached from the archived source's own chat.

import type { ChatInfo, ChatPostprocess } from './types'

export const MEMORY_PASS_KIND = 'memory_pass'

/** `ProjectInfo.kind` of the app-owned per-workspace Memory project. */
const MEMORY_PROJECT_KIND = 'memory'

/** The normalised lifecycle states a pass chat can carry. */
export type MemoryPassState = 'queued' | 'running' | 'done' | 'attention'

type MemoryPassHelper = Extract<
  NonNullable<ChatInfo['helper']>,
  { kind: typeof MEMORY_PASS_KIND }
>

/** The chat's helper when it is a pass, else null. */
function passHelper(chat: { helper?: ChatInfo['helper'] } | null | undefined): MemoryPassHelper | null {
  const helper = chat?.helper
  if (!helper || helper.kind !== MEMORY_PASS_KIND) return null
  return helper
}

export function isMemoryPassChat(chat: { helper?: ChatInfo['helper'] } | null | undefined): boolean {
  return passHelper(chat) !== null
}

/**
 * The pass's own lifecycle state, or '' when *chat* is not a pass. `done` is
 * reachable: a cleanly finished pass is marked done and then archived, and a
 * failed archive POST rolls `archived` back with the state still `done`.
 */
export function memoryPassState(
  chat: { helper?: ChatInfo['helper'] } | null | undefined,
): MemoryPassState | '' {
  return passHelper(chat)?.state ?? ''
}

/** The archived conversation this pass is distilling, with its title. */
export function memoryPassSource(
  chat: { helper?: ChatInfo['helper'] } | null | undefined,
): { chatId: string; title: string } | null {
  const helper = passHelper(chat)
  if (!helper) return null
  return { chatId: helper.source_chat_id, title: helper.source_title || '' }
}

/**
 * True only for a pass that ended unclean. Every other state — queued,
 * running, done — is either still in flight or auto-archived, and neither is
 * something the owner has to act on.
 */
export function memoryPassNeedsAttention(
  chat: { helper?: ChatInfo['helper'] } | null | undefined,
): boolean {
  return passHelper(chat)?.state === 'attention'
}

/** The chat this pass was spawned for, or '' when *chat* is not a pass. */
export function memoryPassSourceChatId(
  chat: { helper?: ChatInfo['helper'] } | null | undefined,
): string {
  return passHelper(chat)?.source_chat_id || ''
}

/**
 * The pass chat an archived source recorded on its own postprocess record, or
 * '' when the source has not spawned one (yet). Guarded on the value's type:
 * `extra` is untyped server data, and a hand-edited record must not put a
 * non-string into `switchChat`.
 */
export function memoryPassChatId(pp: ChatPostprocess | null | undefined): string {
  const id = pp?.steps?.memory_pass?.extra?.chat_id
  return typeof id === 'string' ? id : ''
}

/** True for the app-owned Memory project, which the sidebar never shows. */
export function isMemoryProject(project: { kind?: string } | null | undefined): boolean {
  return project?.kind === MEMORY_PROJECT_KIND
}

