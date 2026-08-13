const CHAT_DRAFTS_STORAGE_KEY = 'ciao-chat-drafts'
const CHAT_SENT_PROMPTS_STORAGE_KEY = 'ciao-chat-sent-prompts'
export const SENT_PROMPT_HISTORY_LIMIT = 50

type DraftStorage = Pick<Storage, 'getItem' | 'setItem' | 'removeItem'>

function defaultStorage(): DraftStorage | null {
  if (typeof localStorage === 'undefined') return null
  return localStorage
}

function readDrafts(storage: DraftStorage): Record<string, string> {
  try {
    const parsed = JSON.parse(storage.getItem(CHAT_DRAFTS_STORAGE_KEY) || '{}')
    if (!parsed || typeof parsed !== 'object' || Array.isArray(parsed)) return {}

    return Object.fromEntries(
      Object.entries(parsed).filter(
        (entry): entry is [string, string] => typeof entry[1] === 'string',
      ),
    )
  } catch {
    return {}
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
  return readDrafts(storage)[chatId] || ''
}

export function writeChatDraft(
  chatId: string | null | undefined,
  text: string,
  storage: DraftStorage | null = defaultStorage(),
): void {
  if (!chatId || !storage) return

  try {
    const drafts = readDrafts(storage)
    if (text) drafts[chatId] = text
    else delete drafts[chatId]

    if (Object.keys(drafts).length) {
      storage.setItem(CHAT_DRAFTS_STORAGE_KEY, JSON.stringify(drafts))
    } else {
      storage.removeItem(CHAT_DRAFTS_STORAGE_KEY)
    }
  } catch {
    // Draft persistence must never prevent typing or sending a message.
  }
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
