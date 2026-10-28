// Settings tabs, in rail order, in two groups: Ciaobot (engine-wide) and the
// active workspace (its name and tile, filled in by the sidebar), which starts
// with its Overview page (/settings/workspace, singular): the active
// workspace's cards followed by the list of all workspaces; there is no other
// workspace page. /settings is the General tab (route kept as-is).
export type SettingsNavItem = { readonly to: string; readonly label: string }
export type SettingsNavGroup = {
  readonly id: 'engine' | 'workspace'
  // null: the sidebar shows the active workspace's name and tile instead.
  readonly label: string | null
  readonly items: readonly SettingsNavItem[]
}

export const SETTINGS_NAV_GROUPS: readonly SettingsNavGroup[] = [
  { id: 'engine', label: 'Ciaobot', items: [
    { to: '/settings', label: 'General' },
    { to: '/settings/appearance', label: 'Appearance' },
    { to: '/settings/access', label: 'Access & devices' },
    { to: '/settings/providers', label: 'Providers' },
    { to: '/settings/advanced', label: 'Advanced' },
  ] },
  { id: 'workspace', label: null, items: [
    { to: '/settings/workspace', label: 'Overview' },
    { to: '/settings/skills', label: 'Skills' },
    { to: '/settings/subagents', label: 'Subagents' },
    { to: '/settings/commands', label: 'Commands' },
    { to: '/settings/mcp', label: 'MCP servers' },
  ] },
]

export const SETTINGS_NAV: readonly SettingsNavItem[] = SETTINGS_NAV_GROUPS.flatMap(group => group.items)

// The label of the tab a settings path shows, for the pane header. A path
// outside the list (an old or unknown tab) falls back to "Settings".
export function settingsTabLabel(path: string): string {
  return SETTINGS_NAV.find(item => item.to === path)?.label ?? 'Settings'
}
