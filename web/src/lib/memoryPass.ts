// Reading the memory pass from the PWA.
//
// The pass runs as an ordinary chat in an app-owned per-workspace system
// project, and the backend has always serialised everything needed to recognise
// one: `ProjectInfo.kind === 'memory'` on the project, and
// `ChatInfo.helper.kind === 'memory_pass'` on the chat it created. Nothing in
// the PWA read those fields, so this is the only place that does — Vue-free, so
// the rules can be tested on their own.
//
// Three facts the UI needs, each deliberately narrow:
//
//   - *is this a pass* — keyed on `kind`, never on `is_system` or a project
//     name. A user project may legitimately be called "Memory"; only the kind
//     is the discriminator.
//   - *does it need the owner* — `state === 'attention'`. A pass that ends
//     cleanly auto-archives and never reaches Home, so this one state is the
//     only thing worth interrupting anyone about.
//   - *which chat is the pass for an archived source* — the id recorded on the
//     source's own `postprocess` record. It is written directly (not folded in
//     from the archive job) because the pass outlives the job by as long as the
//     pass takes.

import type { ChatInfo, ChatPostprocess } from './types'

export const MEMORY_PASS_KIND = 'memory_pass'

/** `ProjectInfo.kind` of the app-owned per-workspace Memory project. */
const MEMORY_PROJECT_KIND = 'memory'

/** What a pass reads as in a list, now that the Memory project is hidden. */
const MEMORY_PASS_LABEL = 'memory pass'

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

/**
 * The project sub-line for a chat row. A pass normally sits in the Memory
 * project, whose name is now hidden, so the row says what the chat actually is
 * rather than naming a project the user cannot find.
 */
export function memoryPassTitle(
  chat: { helper?: ChatInfo['helper'] } | null | undefined,
  projectName: string,
): string {
  return isMemoryPassChat(chat) ? MEMORY_PASS_LABEL : projectName
}
