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
    + 'A restore brings back its files, settings and automations, but not its chat list. '
    + 'Files are kept in .archived-workspaces/ in your Ciaobot folder, '
    + 'and you can restore it from Settings → Workspaces.'
  )
}
