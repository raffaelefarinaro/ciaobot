const CHAT_DRAFTS_STORAGE_KEY = 'ciao-chat-drafts'

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
