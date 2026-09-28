import type { ArchivedWorkspace } from './types'

/**
 * Wording for archiving a workspace (Settings → Workspaces → Archive).
 *
 * Archiving replaced a delete that merged the workspace's notes into the
 * primary one, so the confirmation has to say plainly what happens and what
 * does not: nothing is deleted, nothing is merged, and it can come back.
 */
export function archiveConfirmMessage(name: string): string {
  return (
    `Archive workspace ${name}? It leaves the sidebar and Ciaobot stops using its notes and memory. `
    + 'Its chats are archived and its automations are set aside. '
    + 'A restore brings back its files, settings and automations (paused until you re-enable them), but not its chat list. '
    + 'Files are kept in .archived-workspaces/ in your Ciaobot folder, '
    + 'and you can restore it from Settings → Workspaces.'
  )
}

function plural(count: number, word: string): string {
  return `${count} ${word}${count === 1 ? '' : 's'}`
}

function listOrNone(values: string[] | null | undefined, unset: string): string {
  if (values == null) return unset
  return values.length ? values.join(', ') : 'none'
}

/**
 * Confirmation shown before a restore.
 *
 * The archive's metadata travels through git sync with the notes, so it may
 * have been edited on another device. The server validates every field and
 * this lists what it will apply, so the settings that grant access are seen
 * before anything is restored.
 */
export function restoreConfirmMessage(item: ArchivedWorkspace): string {
  const lines = [`Restore workspace ${item.name} with these settings?`, '']
  lines.push(`Automations: ${plural(item.schedules, 'automation')}, restored paused. Re-enable them in Automations.`)
  if (item.schedules_dropped) {
    lines.push(`${plural(item.schedules_dropped, 'automation')} in the archive did not pass validation and will not be restored.`)
  }
  lines.push(`MCP servers allowed: ${listOrNone(item.allowed_mcp_servers, 'none (default)')}`)
  lines.push(`Extra tools denied: ${listOrNone(item.disallowed_tools, 'workspace default')}`)
  lines.push(`Agent CLI/Runtime: ${item.default_provider || 'claude'}`)
  lines.push(`Google account: ${item.gws_profile || 'none'}`)
  return lines.join('\n')
}

/** Toast after a restore, naming how many automations came back paused. */
export function restoredMessage(name: string, paused: number): string {
  const tail = paused
    ? ` ${plural(paused, 'automation')} restored paused; re-enable ${paused === 1 ? 'it' : 'them'} in Automations.`
    : ''
  return `Workspace "${name}" restored.${tail}`
}
