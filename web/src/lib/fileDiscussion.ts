/** Open a chat about one vault file, with that file pinned beside it.
 *
 * Three surfaces start the same conversation — the file viewer's "Discuss in
 * chat", the sidebar's workspace-guide card, and the retirement queue's "Talk
 * about it" — and each had its own copy of the same four steps: find the
 * workspace's General project, seed a draft, create the chat, pin the file.
 * The copies had already drifted (only one of them said which workspace was
 * missing a General project), so they live here instead.
 *
 * The prompt is seeded as a DRAFT, not sent: these entry points exist so the
 * user can ask their own question about a file before deciding anything, and
 * an auto-sent message spends the first turn on a question they did not ask.
 * The proposal queue's "talk about it" is deliberately the other shape — it
 * runs in the background and reports back.
 */
import { formatFileComments } from './commentContext'
import type { useProjectStore } from '../stores/projects'
import type { ChatInfo } from './types'

type ProjectStore = ReturnType<typeof useProjectStore>

export interface FileDiscussionOptions {
  /** Vault-relative path of the file to talk about and pin. */
  path: string
  /** Opening message, pre-filled into the composer for the user to edit. */
  seed: string
  /** Chat title. Defaults to `Discuss <filename>`. */
  title?: string
  /**
   * Workspace the chat belongs in. Defaults to the active one; naming it
   * matters for a row that carries its own workspace, because a note from
   * work discussed in a personal chat is read against the wrong vault.
   */
  workspace?: string
}

/** The chat that was opened, or null when it could not be (a toast says why). */
export async function startFileDiscussion(
  store: ProjectStore,
  { path, seed, title, workspace }: FileDiscussionOptions,
): Promise<ChatInfo | null> {
  if (!path) return null
  const target = workspace || store.activeWorkspace
  if (target !== store.activeWorkspace) {
    await store.switchWorkspace(target)
  }
  const general = store.projects.find(
    p => p.workspace === target && p.is_auto && p.name === 'General',
  )
  if (!general) {
    store.pushErrorToast('Cannot start chat', `No General project found in the ${target} workspace.`)
    return null
  }
  // Comments left on the file in the viewer are part of what the user wants to
  // say about it, so they ride along with the opening message.
  const pending = store.fileComments[path] ?? []
  const draft = pending.length ? `${seed}\n\n${formatFileComments(pending)}` : seed
  try {
    const chat = await store.createChat(
      general.project_id,
      title || `Discuss ${path.split('/').pop() || path}`,
      draft,
    )
    // Pin it so the chat opens split-view with the file itself visible.
    store.pinFile(chat.chat_id, path)
    return chat
  } catch (e) {
    store.pushErrorToast('Could not start discussion', e instanceof Error ? e.message : String(e))
    return null
  }
}
