/** The chat a workspace keeps open beside the main one (ChatLayout's split), per browser.
 *  A layout choice like `ciao:chat-split-ratio`: phones cannot show it, so it is not engine state. */
export const BESIDE_CHAT_KEY = 'ciao:beside-chat'

function readMap(): Record<string, string> {
  try {
    const raw = typeof localStorage !== 'undefined' ? localStorage.getItem(BESIDE_CHAT_KEY) : null
    const parsed: unknown = raw ? JSON.parse(raw) : {}
    if (!parsed || typeof parsed !== 'object' || Array.isArray(parsed)) return {}
    return Object.fromEntries(
      Object.entries(parsed).filter(([k, v]) => k && typeof v === 'string' && v),
    ) as Record<string, string>
  } catch {
    return {}
  }
}

export function readBesideChat(workspace: string): string {
  return workspace ? readMap()[workspace] || '' : ''
}

export function writeBesideChat(workspace: string, chatId: string): void {
  if (!workspace) return
  const map = readMap()
  if (chatId) map[workspace] = chatId
  else delete map[workspace]
  try {
    if (typeof localStorage === 'undefined') return
    if (Object.keys(map).length) localStorage.setItem(BESIDE_CHAT_KEY, JSON.stringify(map))
    else localStorage.removeItem(BESIDE_CHAT_KEY)
  } catch {
    /* private mode: the split just is not remembered */
  }
}
