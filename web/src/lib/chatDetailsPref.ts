// Whether the user closed Chat details. The choice carries across chats (each
// chat mounts its own panel) and reloads: closing it in one chat keeps it
// closed in the next until it is opened again.
const KEY = 'ciao:chat-details-closed'

export function chatDetailsClosed(): boolean {
  try {
    return typeof localStorage !== 'undefined' && localStorage.getItem(KEY) === '1'
  } catch {
    return false
  }
}

export function rememberChatDetails(open: boolean): void {
  try {
    if (typeof localStorage === 'undefined') return
    if (open) localStorage.removeItem(KEY)
    else localStorage.setItem(KEY, '1')
  } catch {
    // Storage can be unavailable (private mode); the choice then lasts for this panel only.
  }
}
