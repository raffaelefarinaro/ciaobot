import { askArchiveTask } from '../lib/archiveTaskChoice'
import { ARCHIVE_CONFIRM_MESSAGE } from '../lib/archiveCopy'
import { askConfirm } from '../lib/confirm'
import type { ChatInfo } from '../lib/types'
import { useProjectStore } from '../stores/projects'
import { useTaskSignalsStore } from '../stores/taskSignals'

/**
 * The one archive path for the chat header and the sidebar menu. A chat that
 * holds an open board task asks what to do with the task; any other chat gets
 * the plain confirm.
 */
export function useArchiveChat() {
  const store = useProjectStore()
  const taskSignals = useTaskSignalsStore()

  /** Resolves true when the chat was archived (or the server will archive it), false when cancelled. Rejections from `archiveChat` propagate. */
  // `next`: the chat to open in its place (the title rail's neighbour) rather than Home.
  async function archiveChatWithTaskCheck(chat: ChatInfo, options: { next?: string } = {}): Promise<boolean> {
    const task = taskSignals.taskForChat(chat)
    const workspace = taskSignals.loadedWorkspace
    if (task && workspace && task.status !== 'done' && taskSignals.chatHoldsTask(chat, task)) {
      const choice = await askArchiveTask(task, workspace, taskSignals.isAwaitingReview(task))
      if (choice === 'cancel') return false
      // An approval archives the chat on the server (ControlPlane._learn_from_approved),
      // with the approved-task memory pass; archiving here too would race it.
      if (choice === 'approved') {
        if (options.next) await store.switchChat(options.next)
        return true
      }
    } else if (!await askConfirm(ARCHIVE_CONFIRM_MESSAGE, { title: 'Archive chat', confirmLabel: 'Archive' })) {
      return false
    }
    if (options.next) await store.archiveChat(chat.chat_id, { next: options.next })
    else await store.archiveChat(chat.chat_id)
    return true
  }

  return { archiveChatWithTaskCheck }
}
