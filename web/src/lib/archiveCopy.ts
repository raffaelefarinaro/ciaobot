/**
 * Wording for the archive action, shared by the two surfaces that offer it.
 *
 * The chat header button and the sidebar context menu had verbatim copies of
 * the confirm sentence and disagreeing button labels, so the same action read
 * differently depending on where it was started from. One definition keeps them
 * from drifting again on the next edit.
 */

/** Confirm-dialog body. Archiving is reversible, so say so. */
export const ARCHIVE_CONFIRM_MESSAGE =
  'Archive this chat? You can reopen it from the archive.'

/** Label for a full-width button, where there is room to spell it out. */
export const ARCHIVE_ACTION_LABEL = 'Archive chat'

/** Label for a context-menu row, next to Rename and Delete. */
export const ARCHIVE_MENU_LABEL = 'Archive'
