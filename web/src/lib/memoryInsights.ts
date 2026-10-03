// One Home row per archived conversation's memory work.
//
// Archiving a chat queues a memory pass — an ordinary chat of the app's own —
// that distils the transcript into the vault (ciao/web/memory_pass.py). The pass
// used to appear in the normal chat tiers as "Memory pass · <title>", which did
// not say what a person was meant to do.
//
// This is the one row, and the ordinary tiers no longer carry the pass at all
// (the store filters it out of `activeChatsAll`). The row is keyed on the
// archived conversation, and the pass is the thing a person actually opens — a
// question, a permission prompt and the reply all live in that chat. While the
// archive POST is still in flight there is nothing to open but the transcript,
// so the row falls back to it.
//
// Vue-free and store-free: every signal arrives as an argument, so the ordering,
// the phase precedence and the wording are testable without Pinia or a mount.

import { chatActivityTimestamp } from './homeLanes'
import {
  isMemoryPassChat,
  memoryPassSource,
  memoryPassState,
} from './memoryPass'
import type { ChatInfo } from './types'

/**
 * Where one archived conversation's memory work has got to. Grouped by how much
 * it wants the owner: `archiving` / `queued` / `running` are in flight and
 * need nobody, `done` is finished, and only `needsYou` / `attention` are the
 * pass asking for something back.
 */
export type MemoryInsightPhase =
  | 'archiving'
  | 'queued'
  | 'running'
  | 'done'
  | 'attention'
  | 'needsYou'

export interface MemoryInsight {
  /** The archived conversation this row is about. */
  sourceChatId: string
  /** The conversation's own title — the pass's title is an internal label. */
  title: string
  /** The source's activity time, so a busy workspace does not reorder rows. */
  timestamp: string
  phase: MemoryInsightPhase
  /** The sentence the row shows, already phrased for this phase. */
  label: string
  /** The pass chat a click opens, or '' while none exists yet. */
  passChatId: string
  /** The archived transcript, the fallback target before a pass exists. */
  archivePath: string
  /** The question the pass is blocked on, when it is blocked on one. */
  question: string
  /** Waiting on the owner, as opposed to running. */
  blocking: boolean
  /** Still in flight. */
  active: boolean
}

export interface MemoryInsightInput {
  chats: ChatInfo[]
  /** Is this pass blocked on a question, a permission, or a provider retry? */
  needsInput: (chatId: string) => boolean
  /** The first question text of a blocked chat, for the row's second line. */
  pendingQuestion: (chatId: string) => string | null
  /** Is this chat's own archive POST in flight (the optimistic flag)? */
  isArchiving: (chatId: string) => boolean
}

/** The in-flight phases, which need nobody and say so in a muted register. */
const ACTIVE_PHASES = new Set<MemoryInsightPhase>(['archiving', 'queued', 'running'])

/** The two phases where the pass is asking the owner for something. */
const BLOCKING_PHASES = new Set<MemoryInsightPhase>(['needsYou', 'attention'])

/** The row's sentence for a phase that has one fixed phrasing. */
function labelFor(phase: MemoryInsightPhase): string {
  switch (phase) {
    case 'archiving': return 'extracting…'
    case 'queued': return 'queued for memory'
    case 'running': return 'extracting…'
    case 'done': return 'memory updated'
    case 'attention': return 'needs attention'
    case 'needsYou': return 'needs you'
  }
}

export function memoryInsights(input: MemoryInsightInput): MemoryInsight[] {
  const { chats, needsInput, pendingQuestion, isArchiving } = input
  const byId = new Map<string, ChatInfo>()
  for (const chat of chats) byId.set(chat.chat_id, chat)

  const rows = new Map<string, MemoryInsight>()

  function createRow(sourceChatId: string, title: string, timestamp: string, archivePath: string): MemoryInsight {
    return {
      sourceChatId,
      title,
      timestamp,
      archivePath,
      // Provisional and never read back: both loops below set the real phase in
      // the same step they create the row, and a pass then overrides the phase
      // of the row it joins.
      phase: 'archiving',
      label: '',
      passChatId: '',
      question: '',
      blocking: false,
      active: false,
    }
  }

  // The pass first, then the archive side. Order matters beyond taste: the
  // backend truncates activity timestamps to whole seconds, so two
  // conversations archived in the same second tie, and a stable sort would then
  // fall back to whichever loop created the row first. Running the pass first
  // makes a tie resolve the way a reader would order it by hand, and the archive
  // loop below skips a row that already exists rather than clobbering its phase.
  for (const chat of chats) {
    if (chat.archived || !isMemoryPassChat(chat)) continue
    const source = memoryPassSource(chat)
    if (!source) continue
    const origin = byId.get(source.chatId)
    if (!rows.has(source.chatId)) {
      rows.set(source.chatId, origin
        ? createRow(source.chatId, origin.title, chatActivityTimestamp(origin), origin.archive_path || '')
        : createRow(source.chatId, source.title || 'Archived conversation', chatActivityTimestamp(chat), ''))
    }
    const row = rows.get(source.chatId)!

    const state = memoryPassState(chat)
    const blocked = needsInput(chat.chat_id)
    row.passChatId = chat.chat_id
    row.question = blocked ? (pendingQuestion(chat.chat_id) || '') : ''
    if (blocked) row.phase = 'needsYou'
    else if (state === 'attention') row.phase = 'attention'
    else if (state === 'running') row.phase = 'running'
    // A pass marked `done` whose archive POST failed is finished, not queued,
    // and saying otherwise would promise work that will never come.
    else if (state === 'done') row.phase = 'done'
    // Anything else is waiting its turn behind the workspace's one pass slot.
    else row.phase = 'queued'
  }

  // Then every chat whose archive POST is still in flight. A pass never
  // produces a row of its own — its own auto-archive would otherwise show up as
  // a second entry for the same work.
  for (const chat of chats) {
    if (isMemoryPassChat(chat) || !isArchiving(chat.chat_id)) continue
    if (rows.has(chat.chat_id)) continue
    rows.set(chat.chat_id, {
      ...createRow(chat.chat_id, chat.title, chatActivityTimestamp(chat), chat.archive_path || ''),
      phase: 'archiving',
    })
  }

  for (const row of rows.values()) {
    row.blocking = BLOCKING_PHASES.has(row.phase)
    row.active = ACTIVE_PHASES.has(row.phase)
    row.label = labelFor(row.phase)
  }

  return Array.from(rows.values()).sort((a, b) => b.timestamp.localeCompare(a.timestamp))
}

/** "2 updating memory" for a lane's status sentence, or '' when none are. */
export function activeInsightSummary(count: number): string {
  if (count < 1) return ''
  return `${count} updating memory`
}
