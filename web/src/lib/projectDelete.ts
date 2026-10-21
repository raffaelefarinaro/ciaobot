import type { ProjectDeleteReport } from './types'

/**
 * The confirmation text for deleting a project.
 *
 * One builder for both places that can delete a project (the sidebar's `•••`
 * menu and the project page), so the two cannot drift into saying different
 * things about what is about to be lost. A vault-backed project loses a
 * folder of notes from disk, and a link that pointed into it is cleaned up
 * rather than left dangling — both belong in the dialog, because neither is
 * obvious from a button labelled "Delete".
 *
 * The dialog renders the message with `white-space: pre-line`, so the
 * newlines are paragraph breaks.
 */

export interface ProjectDeleteTarget {
  name: string
  vault_folder: string
}

export function projectDeleteMessage(project: ProjectDeleteTarget, chatCount: number): string {
  const lines: string[] = [`Delete "${project.name}"?`]
  if (project.vault_folder) {
    lines.push(
      `Its vault folder projects/active/${project.vault_folder}/ is deleted, and the links pointing into it are cleaned up.`,
    )
  }
  lines.push(
    chatCount === 1
      ? 'Its 1 chat is archived.'
      : `Its ${chatCount} chats are archived.`,
  )
  lines.push('This cannot be undone.')
  return lines.join('\n\n')
}

/**
 * The toast body after a delete: what the cleanup actually removed.
 *
 * The dialog said what would happen; this says what did, so a user who
 * deleted a folder of notes knows how much of it was there.
 */
export function projectDeleteToastBody(report: ProjectDeleteReport): string {
  if (!report.vault_entry_removed) {
    return 'The project and its chats are gone.'
  }
  const parts = [
    report.removed_notes === 1
      ? '1 note removed'
      : `${report.removed_notes} notes removed`,
    report.removed_files === 1
      ? '1 file removed'
      : `${report.removed_files} files removed`,
  ]
  if (report.edited_backlinks.length) {
    parts.push(
      report.edited_backlinks.length === 1
        ? '1 link cleaned'
        : `${report.edited_backlinks.length} links cleaned`,
    )
  }
  return `${parts.join(', ')}.`
}
