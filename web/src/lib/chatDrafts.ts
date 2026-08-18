const CHAT_DRAFTS_STORAGE_KEY = 'ciao-chat-drafts'
const CHAT_SENT_PROMPTS_STORAGE_KEY = 'ciao-chat-sent-prompts'
export const SENT_PROMPT_HISTORY_LIMIT = 50
// A draft this old is no longer offered for recovery when its chat is gone.
const ORPHAN_DRAFT_MAX_AGE_MS = 14 * 24 * 60 * 60 * 1000

type DraftStorage = Pick<Storage, 'getItem' | 'setItem' | 'removeItem'>

interface DraftEntry {
  text: string
  projectId: string
  updatedAt: number
}

export interface OrphanDraft {
  chatId: string
  text: string
  projectId: string
  updatedAt: number
}

function defaultStorage(): DraftStorage | null {
  if (typeof localStorage === 'undefined') return null
  return localStorage
}

// Accepts both the current object shape and the legacy plain-string shape
// (pre-#277: `Record<chatId, string>`). A legacy string is stamped with the
// current time on read — it gets one fair chance at the orphan-recovery TTL
// rather than being treated as permanently ageless or instantly expired.
// Anything else (number, array, null, an object missing `text`) is dropped,
// matching the previous "ignore malformed values" contract.
function readDrafts(storage: DraftStorage): Record<string, DraftEntry> {
  try {
    const parsed = JSON.parse(storage.getItem(CHAT_DRAFTS_STORAGE_KEY) || '{}')
    if (!parsed || typeof parsed !== 'object' || Array.isArray(parsed)) return {}

    const out: Record<string, DraftEntry> = {}
    for (const [chatId, value] of Object.entries(parsed)) {
      if (typeof value === 'string') {
        if (value) out[chatId] = { text: value, projectId: '', updatedAt: Date.now() }
      } else if (value && typeof value === 'object' && typeof (value as { text?: unknown }).text === 'string') {
        const entry = value as Partial<DraftEntry>
        if (entry.text) {
          out[chatId] = {
            text: entry.text,
            projectId: typeof entry.projectId === 'string' ? entry.projectId : '',
            updatedAt: typeof entry.updatedAt === 'number' ? entry.updatedAt : Date.now(),
          }
        }
      }
    }
    return out
  } catch {
    return {}
  }
}

function writeDrafts(storage: DraftStorage, drafts: Record<string, DraftEntry>): void {
  if (Object.keys(drafts).length) {
    storage.setItem(CHAT_DRAFTS_STORAGE_KEY, JSON.stringify(drafts))
  } else {
    storage.removeItem(CHAT_DRAFTS_STORAGE_KEY)
  }
}

function readSentPromptHistories(storage: DraftStorage): Record<string, string[]> {
  try {
    const parsed = JSON.parse(storage.getItem(CHAT_SENT_PROMPTS_STORAGE_KEY) || '{}')
    if (!parsed || typeof parsed !== 'object' || Array.isArray(parsed)) return {}

    return Object.fromEntries(
      Object.entries(parsed)
        .map(([chatId, prompts]) => {
          if (!Array.isArray(prompts)) return null
          const uniquePrompts = prompts.filter(
            (prompt): prompt is string => typeof prompt === 'string' && prompt.length > 0,
          ).filter((prompt, index, all) => all.indexOf(prompt) === index)
          return [chatId, uniquePrompts.slice(-SENT_PROMPT_HISTORY_LIMIT)] as const
        })
        .filter((entry): entry is readonly [string, string[]] => entry !== null),
    )
  } catch {
    return {}
  }
}

function writeSentPromptHistories(
  storage: DraftStorage,
  histories: Record<string, string[]>,
): void {
  const nonEmptyHistories = Object.fromEntries(
    Object.entries(histories).filter(([, prompts]) => prompts.length > 0),
  )
  if (Object.keys(nonEmptyHistories).length) {
    storage.setItem(CHAT_SENT_PROMPTS_STORAGE_KEY, JSON.stringify(nonEmptyHistories))
  } else {
    storage.removeItem(CHAT_SENT_PROMPTS_STORAGE_KEY)
  }
}

export function readChatDraft(
  chatId: string | null | undefined,
  storage: DraftStorage | null = defaultStorage(),
): string {
  if (!chatId || !storage) return ''
  return readDrafts(storage)[chatId]?.text || ''
}

export function writeChatDraft(
  chatId: string | null | undefined,
  text: string,
  storage: DraftStorage | null = defaultStorage(),
  opts?: { projectId?: string },
): void {
  if (!chatId || !storage) return

  try {
    const drafts = readDrafts(storage)
    if (text) {
      drafts[chatId] = {
        text,
        projectId: opts?.projectId ?? drafts[chatId]?.projectId ?? '',
        updatedAt: Date.now(),
      }
    } else {
      delete drafts[chatId]
    }
    writeDrafts(storage, drafts)
  } catch {
    // Draft persistence must never prevent typing or sending a message.
  }
}

/** Discard one chat's draft — used once its text has a new home, or on explicit dismissal. */
export function clearChatDraft(
  chatId: string | null | undefined,
  storage: DraftStorage | null = defaultStorage(),
): void {
  writeChatDraft(chatId, '', storage)
}

/**
 * Drafts whose chat no longer exists (e.g. swept as an abandoned empty
 * chat) and are still within the recovery window. `validChatIds` is
 * authoritative: whatever the caller currently knows to be a real chat.
 */
export function readOrphanCandidates(
  validChatIds: Set<string>,
  storage: DraftStorage | null = defaultStorage(),
): OrphanDraft[] {
  if (!storage) return []
  const now = Date.now()
  const out: OrphanDraft[] = []
  for (const [chatId, entry] of Object.entries(readDrafts(storage))) {
    if (validChatIds.has(chatId)) continue
    if (now - entry.updatedAt > ORPHAN_DRAFT_MAX_AGE_MS) continue
    out.push({ chatId, text: entry.text, projectId: entry.projectId, updatedAt: entry.updatedAt })
  }
  return out
}

/** Return sent prompts from oldest to newest for one chat session. */
export function readSentPromptHistory(
  chatId: string | null | undefined,
  storage: DraftStorage | null = defaultStorage(),
): string[] {
  if (!chatId || !storage) return []
  return readSentPromptHistories(storage)[chatId] || []
}

/** Add one sent prompt, keeping the session history bounded and deduplicated. */
export function recordSentPrompt(
  chatId: string | null | undefined,
  text: string,
  storage: DraftStorage | null = defaultStorage(),
): void {
  const prompt = text.trim()
  if (!chatId || !storage || !prompt) return

  try {
    const history = readSentPromptHistories(storage)
    const existing = history[chatId] || []
    history[chatId] = [...existing.filter((entry) => entry !== prompt), prompt].slice(-SENT_PROMPT_HISTORY_LIMIT)
    writeSentPromptHistories(storage, history)
  } catch {
    // History persistence must never prevent sending a message.
  }
}
