// Settings tabs, in rail order. /settings is the General tab (route kept as-is).
export const SETTINGS_NAV = [
  { to: '/settings', label: 'General' },
  { to: '/settings/workspaces', label: 'Workspaces' },
  { to: '/settings/models', label: 'Models & providers' },
  { to: '/settings/skills', label: 'Skills' },
  { to: '/settings/subagents', label: 'Subagents' },
  { to: '/settings/commands', label: 'Commands' },
  { to: '/settings/mcp', label: 'MCP servers' },
] as const

// The label of the tab a settings path shows, for the pane header. A path
// outside the list (an old or unknown tab) falls back to "Settings".
export function settingsTabLabel(path: string): string {
  return SETTINGS_NAV.find(item => item.to === path)?.label ?? 'Settings'
}
