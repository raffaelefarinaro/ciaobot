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

/** Confirm-dialog body. Names the cascade so it is never a surprise. */
export function archiveConfirmMessage(count: number): string {
  return count
    ? `Archive this chat and ${subchats(count)}? You can reopen them from the archive.`
    : 'Archive this chat? You can reopen it from the archive.'
}

/** Label for a full-width button, where there is room to spell it out. */
export function archiveActionLabel(count: number): string {
  return count ? `Archive chat and ${subchats(count)}` : 'Archive chat'
}

/** Label for a context-menu row, where the parenthetical reads better. */
export function archiveMenuLabel(count: number): string {
  return count ? `Archive (also ${subchats(count)})` : 'Archive'
}
