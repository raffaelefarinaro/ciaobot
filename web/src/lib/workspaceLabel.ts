/** Human-readable label for a workspace's internal (kebab/underscore) name. */
export function workspaceLabel(name: string): string {
  if (!name) return 'Workspace'
  return name
    .split(/[-_\s]+/)
    .filter(Boolean)
    .map(part => part.charAt(0).toUpperCase() + part.slice(1))
    .join(' ')
}
