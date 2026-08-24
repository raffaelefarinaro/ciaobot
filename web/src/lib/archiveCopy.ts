/**
 * Wording for the archive action, shared by the two surfaces that offer it.
 *
 * The chat header button and the sidebar context menu had verbatim copies of
 * the confirm sentence and disagreeing button labels, so the same action read
 * differently depending on where it was started from. One definition keeps them
 * from drifting again on the next edit.
 *
 * `count` is the number of active delegate subchats the user can actually see —
 * `store.activeDelegatesFor(id).length`, which excludes remote sessions.
 */

function subchats(count: number): string {
  return `${count} subchat${count === 1 ? '' : 's'}`
}

/**
 * Confirm-dialog body. Names the cascade so it is never a surprise.
 *
 * `busyCount` is how many of those subchats are working right now (streaming a
 * turn or running background agents). Archiving does not wait for them — it
 * stops them — so the dialog has to say that before the user commits, not
 * afterwards. Without it the only way to find out was to lose the work.
 */
export function archiveConfirmMessage(count: number, busyCount = 0): string {
  if (!count) return 'Archive this chat? You can reopen it from the archive.'
  const reopen = 'You can reopen them from the archive.'
  if (!busyCount) {
    return `Archive this chat and ${subchats(count)}? ${reopen}`
  }
  const working = busyCount === 1 ? 'is still working' : 'are still working'
  const unfinished = busyCount === 1 ? 'its unfinished work' : 'their unfinished work'
  return (
    `Archive this chat and ${subchats(count)}? `
    + `${subchats(busyCount)} ${working} and will be stopped, so ${unfinished} is lost. `
    + reopen
  )
}

/**
 * Toast shown after the server reports that it stopped running subchats.
 *
 * The confirm dialog warns from the client's view of who looked busy; this
 * reports what actually happened server-side, which is not the same set (a
 * turn can start or end in between, and remote delegates never show up in the
 * client's count at all).
 */
export function archiveStoppedToast(count: number): { title: string; body: string } {
  return {
    title: `Stopped ${subchats(count)} mid-turn`,
    body:
      count === 1
        ? 'Archiving this chat stopped a subchat that was still working. Whatever that turn had not finished is not in the archive.'
        : `Archiving this chat stopped ${count} subchats that were still working. Whatever those turns had not finished is not in the archive.`,
  }
}

/** Toast body for subchats the server could not archive. They are still live. */
export function archiveFailedToast(count: number): { title: string; body: string } {
  return {
    title: 'Some subchats were not archived',
    body:
      count === 1
        ? '1 subchat could not be archived and is still active. Archive it directly from its own chat.'
        : `${count} subchats could not be archived and are still active. Archive them directly from their own chats.`,
  }
}

/** Label for a full-width button, where there is room to spell it out. */
export function archiveActionLabel(count: number): string {
  return count ? `Archive chat and ${subchats(count)}` : 'Archive chat'
}

/** Label for a context-menu row, where the parenthetical reads better. */
export function archiveMenuLabel(count: number): string {
  return count ? `Archive (also ${subchats(count)})` : 'Archive'
}
